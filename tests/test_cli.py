import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from scrutiny.cli import main


def _run(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def repo(tmp_path):
    _run(tmp_path, "init", "-q")
    _run(tmp_path, "config", "user.email", "test@test.com")
    _run(tmp_path, "config", "user.name", "test")
    (tmp_path / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    _run(tmp_path, "add", "math_utils.py")
    _run(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class _ScriptedMessages:
    """Routes to a different canned response depending on which tool is being
    forced -- report_findings (the review pass) vs write_repro_test (the
    verify pass) -- so one fake client can drive the whole CLI flow."""

    def __init__(self, review_findings, repro_response=None):
        self.review_findings = review_findings
        self.repro_response = repro_response or {
            "feasible": False,
            "infeasible_reason": "not scripted",
        }
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool_name = kwargs["tool_choice"]["name"]
        if tool_name == "report_findings":
            payload = {"findings": self.review_findings}
        else:
            payload = self.repro_response
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=payload)])


class _FakeAnthropic:
    def __init__(self, review_findings, repro_response=None):
        self.messages = _ScriptedMessages(review_findings, repro_response)

    def __call__(self, *args, **kwargs):
        return self


def _patch_anthropic(monkeypatch, review_findings, repro_response=None):
    fake = _FakeAnthropic(review_findings, repro_response)
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return fake


def test_review_help_exits_cleanly():
    result = CliRunner().invoke(main, ["review", "--help"])
    assert result.exit_code == 0
    assert "TARGET" in result.output


def test_review_fails_cleanly_without_an_api_key(repo, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = CliRunner().invoke(main, ["review", str(repo)])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in str(result.output) + str(result.exception)


def test_review_reports_no_findings_when_llm_finds_nothing(repo, monkeypatch):
    (repo / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    _patch_anthropic(monkeypatch, review_findings=[])

    result = CliRunner().invoke(main, ["review", str(repo)])
    assert result.exit_code == 0
    assert "No findings." in result.output


def test_review_confirms_a_real_bug_end_to_end(repo, monkeypatch):
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a - b\n")
    finding = {
        "line": 2,
        "summary": "add() subtracts instead of adding",
        "failure_scenario": "add(2, 3) should be 5 but returns -1",
        "severity": "high",
    }
    repro = {
        "feasible": True,
        "test_code": (
            f"import sys; sys.path.insert(0, {str(repo)!r})\n"
            "import math_utils\n\n"
            "def test_add():\n"
            "    assert math_utils.add(2, 3) == 5\n"
        ),
    }
    _patch_anthropic(monkeypatch, review_findings=[finding], repro_response=repro)

    result = CliRunner().invoke(main, ["review", str(repo)])
    assert result.exit_code == 1
    assert "[CONFIRMED]" in result.output
    assert "add() subtracts instead of adding" in result.output


def test_review_drops_findings_refuted_by_verification(repo, monkeypatch):
    (repo / "math_utils.py").write_text(
        "def add(a, b):\n    # sums two numbers\n    return a + b\n"
    )
    finding = {
        "line": 2,
        "summary": "add() is broken",
        "failure_scenario": "add(2, 3) should be 5 but isn't",
        "severity": "high",
    }
    repro = {
        "feasible": True,
        "test_code": (
            f"import sys; sys.path.insert(0, {str(repo)!r})\n"
            "import math_utils\n\n"
            "def test_add():\n"
            "    assert math_utils.add(2, 3) == 5\n"
        ),
    }
    _patch_anthropic(monkeypatch, review_findings=[finding], repro_response=repro)

    result = CliRunner().invoke(main, ["review", str(repo)])
    assert result.exit_code == 0
    assert "No findings." in result.output


def test_no_verify_flag_skips_verification_and_reports_plausible(repo, monkeypatch):
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a - b\n")
    finding = {
        "line": 2,
        "summary": "add() subtracts instead of adding",
        "failure_scenario": "add(2, 3) should be 5 but returns -1",
        "severity": "high",
    }
    fake = _patch_anthropic(monkeypatch, review_findings=[finding])

    result = CliRunner().invoke(main, ["review", str(repo), "--no-verify"])
    assert result.exit_code == 0
    assert "[PLAUSIBLE]" in result.output
    assert "verification skipped" in result.output
    # Only the review call happened, never a repro-generation call.
    assert len(fake.messages.calls) == 1
