from types import SimpleNamespace

from scrutiny.review import Finding
from scrutiny.verify import (
    classify_repro_result,
    find_enclosing_function,
    run_repro,
    verify_finding,
)


class _FakeMessages:
    def __init__(self, response_input):
        self.response_input = response_input
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool_use_block = SimpleNamespace(type="tool_use", input=self.response_input)
        return SimpleNamespace(content=[tool_use_block])


class _FakeClient:
    def __init__(self, response_input):
        self.messages = _FakeMessages(response_input)


def test_find_enclosing_function_picks_the_innermost_match():
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        return 1\n"
        "    return inner()\n"
        "\n"
        "def sibling():\n"
        "    return 2\n"
    )
    assert find_enclosing_function(source, 3) == "inner"
    assert find_enclosing_function(source, 4) == "outer"
    assert find_enclosing_function(source, 7) == "sibling"


def test_find_enclosing_function_returns_none_for_module_level_code():
    source = "x = 1\ny = 2\n"
    assert find_enclosing_function(source, 1) is None


def test_run_repro_and_classify_confirms_a_genuine_bug():
    # Simulates an LLM-written repro against a function that's actually wrong:
    # off-by-one causes add(2, 3) to return 4 instead of 5.
    test_code = (
        "def add(a, b):\n    return a + b - 1\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    returncode, output = run_repro(test_code)
    verdict, _ = classify_repro_result(returncode, output)
    assert verdict == "CONFIRMED"
    assert "AssertionError" in output


def test_run_repro_and_classify_refutes_a_false_positive():
    test_code = "def add(a, b):\n    return a + b\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    returncode, output = run_repro(test_code)
    verdict, _ = classify_repro_result(returncode, output)
    assert verdict == "REFUTED"


def test_run_repro_and_classify_is_plausible_on_unrelated_error():
    test_code = "def test_add():\n    import nonexistent_module_xyz\n"
    returncode, output = run_repro(test_code)
    verdict, _ = classify_repro_result(returncode, output)
    assert verdict == "PLAUSIBLE"


def test_verify_finding_end_to_end_confirms_a_real_bug(tmp_path):
    module_file = tmp_path / "math_utils.py"
    module_file.write_text("def add(a, b):\n    return a - b\n")

    finding = Finding(
        file="math_utils.py",
        line=2,
        summary="add() subtracts instead of adding",
        failure_scenario="add(2, 3) should be 5 but returns -1",
        severity="high",
    )
    llm_response = {
        "feasible": True,
        "test_code": (
            "import sys\n"
            f"sys.path.insert(0, {str(tmp_path)!r})\n"
            "import math_utils\n\n"
            "def test_add():\n"
            "    assert math_utils.add(2, 3) == 5\n"
        ),
    }
    client = _FakeClient(llm_response)

    result = verify_finding(client, finding, tmp_path)
    assert result.verdict == "CONFIRMED"


def test_verify_finding_skips_non_python_files(tmp_path):
    finding = Finding(file="notes.txt", line=1, summary="x", failure_scenario="y", severity="low")
    result = verify_finding(_FakeClient({}), finding, tmp_path)
    assert result.verdict == "PLAUSIBLE"
    assert "Python" in result.reason


def test_verify_finding_respects_llm_infeasibility_call(tmp_path):
    module_file = tmp_path / "network_client.py"
    module_file.write_text("def fetch(url):\n    return None\n")
    finding = Finding(
        file="network_client.py",
        line=2,
        summary="fetch always returns None",
        failure_scenario="a real network call would be needed to verify this",
        severity="medium",
    )
    client = _FakeClient({"feasible": False, "infeasible_reason": "requires a live network call"})
    result = verify_finding(client, finding, tmp_path)
    assert result.verdict == "PLAUSIBLE"
    assert result.reason == "requires a live network call"
