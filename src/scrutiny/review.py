"""LLM review pass: sends each changed file's diff to Claude and asks for
concrete correctness bugs, returned via forced tool-use so the output is
structured JSON rather than free text to parse.

This pass alone is exactly what most existing AI code-review tools ship --
the differentiator in this project is the verification pass in verify.py,
which actually tries to prove a finding is real before calling it CONFIRMED.
"""

from __future__ import annotations

from dataclasses import dataclass

from scrutiny.diffs import FileDiff

DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """You are a rigorous code reviewer. Review ONLY the diff \
provided, looking for genuine correctness bugs: crashes, wrong results, \
security vulnerabilities, resource leaks, race conditions, off-by-one \
errors, and broken edge cases. Do NOT report style, formatting, naming, or \
missing-comment issues -- those are not bugs.

If you find nothing, report zero findings. Do not invent an issue just to \
have something to say.

Every finding must include a CONCRETE failure_scenario: specific input \
values or conditions that would trigger the bug, and what goes wrong as a \
result. A finding without a concrete scenario is not useful and should be \
omitted."""

REPORT_FINDINGS_TOOL = {
    "name": "report_findings",
    "description": "Report the concrete correctness bugs found in this diff.",
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line": {
                            "type": ["integer", "null"],
                            "description": "New-file line number the bug is anchored to, if any.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "One-sentence statement of the bug.",
                        },
                        "failure_scenario": {
                            "type": "string",
                            "description": "Concrete inputs that trigger it, and what goes wrong.",
                        },
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["summary", "failure_scenario", "severity"],
                },
            }
        },
        "required": ["findings"],
    },
}


@dataclass(frozen=True)
class Finding:
    file: str
    line: int | None
    summary: str
    failure_scenario: str
    severity: str


def _render_file_diff(file_diff: FileDiff) -> str:
    return "\n".join(hunk.text for hunk in file_diff.hunks)


def review_file_diff(client, file_diff: FileDiff, model: str = DEFAULT_MODEL) -> list[Finding]:
    if not file_diff.hunks:
        return []

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[REPORT_FINDINGS_TOOL],
        tool_choice={"type": "tool", "name": "report_findings"},
        messages=[
            {
                "role": "user",
                "content": f"File: {file_diff.path}\n\n{_render_file_diff(file_diff)}",
            }
        ],
    )

    tool_use = next(block for block in response.content if block.type == "tool_use")
    raw_findings = tool_use.input.get("findings", [])
    return [
        Finding(
            file=file_diff.path,
            line=f.get("line"),
            summary=f["summary"],
            failure_scenario=f["failure_scenario"],
            severity=f["severity"],
        )
        for f in raw_findings
    ]


def review_diff(client, file_diffs: list[FileDiff], model: str = DEFAULT_MODEL) -> list[Finding]:
    findings = []
    for file_diff in file_diffs:
        findings.extend(review_file_diff(client, file_diff, model))
    return findings
