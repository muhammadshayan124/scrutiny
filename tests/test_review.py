from types import SimpleNamespace

from scrutiny.diffs import FileDiff, Hunk
from scrutiny.review import Finding, review_diff, review_file_diff


class _FakeMessages:
    """Stands in for client.messages -- scripted to return a canned tool_use
    block shaped exactly like the real Anthropic SDK response, so review.py's
    parsing logic is exercised for real without a network call."""

    def __init__(self, findings_by_call):
        self._findings_by_call = list(findings_by_call)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        findings = self._findings_by_call.pop(0)
        tool_use_block = SimpleNamespace(type="tool_use", input={"findings": findings})
        return SimpleNamespace(content=[tool_use_block])


class _FakeClient:
    def __init__(self, findings_by_call):
        self.messages = _FakeMessages(findings_by_call)


def _make_file_diff(path="app.py"):
    return FileDiff(
        path=path,
        is_new_file=False,
        is_deleted_file=False,
        hunks=[Hunk(header="@@ -1,2 +1,2 @@", text="-old\n+new\n", added_line_numbers=[1])],
    )


def test_review_file_diff_parses_findings_from_the_tool_call():
    client = _FakeClient(
        [
            [
                {
                    "line": 5,
                    "summary": "off-by-one in loop bound",
                    "failure_scenario": "calling f([1,2,3]) skips the last element",
                    "severity": "medium",
                }
            ]
        ]
    )
    findings = review_file_diff(client, _make_file_diff())
    assert findings == [
        Finding(
            file="app.py",
            line=5,
            summary="off-by-one in loop bound",
            failure_scenario="calling f([1,2,3]) skips the last element",
            severity="medium",
        )
    ]


def test_review_file_diff_returns_empty_list_when_no_bugs_found():
    client = _FakeClient([[]])
    findings = review_file_diff(client, _make_file_diff())
    assert findings == []


def test_review_file_diff_skips_files_with_no_hunks_without_calling_the_api():
    client = _FakeClient([])
    empty_diff = FileDiff(path="renamed.py", is_new_file=False, is_deleted_file=False, hunks=[])
    findings = review_file_diff(client, empty_diff)
    assert findings == []
    assert client.messages.calls == []


def test_review_diff_aggregates_findings_across_multiple_files():
    client = _FakeClient(
        [
            [{"line": 1, "summary": "bug A", "failure_scenario": "scenario A", "severity": "high"}],
            [{"line": 2, "summary": "bug B", "failure_scenario": "scenario B", "severity": "low"}],
        ]
    )
    findings = review_diff(client, [_make_file_diff("a.py"), _make_file_diff("b.py")])
    assert [f.file for f in findings] == ["a.py", "b.py"]
    assert [f.summary for f in findings] == ["bug A", "bug B"]


def test_review_file_diff_sends_the_diff_text_in_the_prompt():
    client = _FakeClient([[]])
    review_file_diff(client, _make_file_diff("app.py"))
    sent_content = client.messages.calls[0]["messages"][0]["content"]
    assert "app.py" in sent_content
    assert "+new" in sent_content
