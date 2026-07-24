"""The actual differentiator: instead of asking the LLM how confident it is
in a finding, generate a concrete repro for it and run it.

Convention: the generated pytest test asserts the CORRECT expected behavior
for the specific inputs in the finding's failure_scenario. If the described
bug is real, running it against the actual (buggy) code makes the test FAIL.
If the finding was wrong, the test PASSES -- which we treat as evidence
against the finding, not just "unverified."

This only works for Python, and only for bugs isolable as a single-function
unit test (no live network/DB/timing dependencies) -- see the README for why
that's a real, disclosed limitation rather than a rounding error.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from scrutiny.review import Finding

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT_SECONDS = 15

WRITE_REPRO_TOOL = {
    "name": "write_repro_test",
    "description": "Write a pytest test checking the correct expected behavior for a bug.",
    "input_schema": {
        "type": "object",
        "properties": {
            "feasible": {
                "type": "boolean",
                "description": "Whether a self-contained unit-level repro can actually test this.",
            },
            "infeasible_reason": {
                "type": "string",
                "description": "If not feasible, why (e.g. needs a live network call, DB, timing).",
            },
            "test_code": {
                "type": "string",
                "description": (
                    "A complete, self-contained pytest test function (plus imports) asserting the "
                    "CORRECT expected behavior for the specific inputs in the failure scenario. If "
                    "the bug is real, this test will fail against the actual code."
                ),
            },
        },
        "required": ["feasible"],
    },
}


@dataclass(frozen=True)
class VerificationResult:
    verdict: str  # "CONFIRMED", "REFUTED", or "PLAUSIBLE"
    reason: str
    repro_code: str | None = None
    repro_output: str | None = None


def find_enclosing_function(source: str, line: int) -> str | None:
    tree = ast.parse(source)
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
            if start <= line <= end and (
                best is None or (end - start) < (best.end_lineno - best.lineno)
            ):
                best = node
    return best.name if best else None


def generate_repro(
    client, finding: Finding, file_path: Path, function_name: str, model: str = DEFAULT_MODEL
) -> dict:
    module_name = file_path.stem
    prompt = (
        f"A code reviewer flagged a possible bug in `{function_name}` "
        f"(module `{module_name}`, file `{file_path.name}`):\n\n"
        f"Summary: {finding.summary}\n"
        f"Failure scenario: {finding.failure_scenario}\n\n"
        f"Write a pytest test to check whether this is real. The test must:\n"
        f"- start with `import sys; sys.path.insert(0, {str(file_path.parent)!r})` "
        f"then `import {module_name}`\n"
        f"- call `{module_name}.{function_name}(...)` with the SPECIFIC inputs from the "
        f"failure scenario above\n"
        f"- assert the CORRECT expected behavior, not the buggy one\n\n"
        f"If this can't be checked with a simple, self-contained unit test (needs a live "
        f"network call, a database, real timing/concurrency), set feasible=false instead."
    )
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        tools=[WRITE_REPRO_TOOL],
        tool_choice={"type": "tool", "name": "write_repro_test"},
        messages=[{"role": "user", "content": prompt}],
    )
    tool_use = next(block for block in response.content if block.type == "tool_use")
    return tool_use.input


def run_repro(test_code: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_path = Path(tmp_dir) / "test_repro.py"
        test_path.write_text(test_code, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(test_path), "-q"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=tmp_dir,
            )
            return result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return -1, "repro execution timed out"


def classify_repro_result(returncode: int, output: str) -> tuple[str, str]:
    if returncode == 0:
        return "REFUTED", "repro ran and did not reproduce the described failure"
    if returncode == 1 and "AssertionError" in output:
        return "CONFIRMED", "repro genuinely fails as predicted"
    return (
        "PLAUSIBLE",
        "verification inconclusive (repro errored for reasons unrelated to the finding)",
    )


def verify_finding(
    client, finding: Finding, repo_path: Path, model: str = DEFAULT_MODEL
) -> VerificationResult:
    file_path = repo_path / finding.file

    if file_path.suffix != ".py":
        return VerificationResult(
            "PLAUSIBLE", "verification is only supported for Python files in v1"
        )
    if finding.line is None:
        return VerificationResult(
            "PLAUSIBLE", "finding has no line number to locate a function from"
        )
    if not file_path.exists():
        return VerificationResult("PLAUSIBLE", f"could not read {finding.file} to verify")

    source = file_path.read_text(encoding="utf-8")
    function_name = find_enclosing_function(source, finding.line)
    if function_name is None:
        return VerificationResult(
            "PLAUSIBLE", "could not isolate a specific function to verify against"
        )

    repro = generate_repro(client, finding, file_path, function_name, model)
    if not repro.get("feasible", False):
        reason = repro.get("infeasible_reason") or "verification not feasible for this finding"
        return VerificationResult("PLAUSIBLE", reason)

    test_code = repro.get("test_code", "")
    if not test_code:
        return VerificationResult("PLAUSIBLE", "model reported feasible but produced no test code")

    returncode, output = run_repro(test_code)
    verdict, reason = classify_repro_result(returncode, output)
    return VerificationResult(verdict, reason, repro_code=test_code, repro_output=output)
