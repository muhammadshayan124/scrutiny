import subprocess

import pytest

from scrutiny.diffs import DiffFetchError, get_local_diff, parse_diff


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


def test_get_local_diff_returns_real_git_diff_output(repo):
    (repo / "math_utils.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    diff_text = get_local_diff(str(repo), [])
    assert "math_utils.py" in diff_text
    assert "-    return a + b" in diff_text
    assert "+    return a - b" in diff_text


def test_get_local_diff_raises_on_invalid_repo(tmp_path):
    with pytest.raises(DiffFetchError):
        get_local_diff(str(tmp_path), [])


def test_parse_diff_extracts_path_and_added_line_numbers(repo):
    (repo / "math_utils.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n"
    )
    diff_text = get_local_diff(str(repo), [])

    files = parse_diff(diff_text)
    assert len(files) == 1
    assert files[0].path == "math_utils.py"
    assert not files[0].is_new_file
    assert not files[0].is_deleted_file

    hunk = files[0].hunks[0]
    # New file line numbers: 1=def add, 2=return a - b, 3=blank, 4=blank, 5=def mul, 6=return a*b
    assert hunk.added_line_numbers == [2, 3, 4, 5, 6]
    assert "return a - b" in hunk.text
    assert "def mul(a, b):" in hunk.text


def test_parse_diff_flags_new_file(repo):
    (repo / "new_module.py").write_text("x = 1\n")
    diff_text = get_local_diff(str(repo), ["--no-index", "/dev/null", "new_module.py"])
    files = parse_diff(diff_text)
    assert len(files) == 1
    assert files[0].is_new_file
