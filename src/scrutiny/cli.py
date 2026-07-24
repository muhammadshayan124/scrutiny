from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from scrutiny.diffs import DiffFetchError, get_local_diff, get_pr_diff, parse_diff
from scrutiny.review import DEFAULT_MODEL, review_diff
from scrutiny.verify import verify_finding

SAFETY_NOTICE = (
    "scrutiny executes LLM-generated repro code against your codebase to verify findings. "
    "It runs in a temp directory with a timeout, but it is NOT a sandbox -- don't run this "
    "against diffs you don't already trust the source of. Review CONFIRMED repros yourself "
    "before acting on them; verification reduces false positives, it doesn't eliminate them."
)


@click.group()
def main() -> None:
    """scrutiny -- reviews a diff with an LLM, then tries to actually prove each finding."""


@main.command()
@click.argument("target", default=".")
@click.option(
    "--base",
    default=None,
    help="Local mode: base ref to diff against (default: working tree changes).",
)
@click.option("--model", default=DEFAULT_MODEL, help="Anthropic model to use.")
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip execution-based verification; report raw LLM findings only.",
)
@click.option(
    "--json-out",
    type=click.Path(),
    default=None,
    help="Write the full report as JSON to this path.",
)
def review(
    target: str, base: str | None, model: str, no_verify: bool, json_out: str | None
) -> None:
    """Review TARGET: a local git repo path, or a GitHub PR URL."""
    click.echo(SAFETY_NOTICE, err=True)

    try:
        import anthropic
    except ImportError:
        raise click.ClickException(
            "anthropic is not installed. Install it with: pip install scrutiny"
        ) from None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise click.ClickException("ANTHROPIC_API_KEY is not set.")

    client = anthropic.Anthropic()

    is_pr = target.startswith("http")
    if is_pr:
        try:
            diff_text = get_pr_diff(target)
        except DiffFetchError as exc:
            raise click.ClickException(str(exc)) from exc
        repo_path = None
    else:
        diff_args = [base] if base else []
        try:
            diff_text = get_local_diff(target, diff_args)
        except DiffFetchError as exc:
            raise click.ClickException(str(exc)) from exc
        repo_path = Path(target)

    file_diffs = parse_diff(diff_text)
    if not file_diffs:
        click.echo("No changes to review.")
        return

    click.echo(f"Reviewing {len(file_diffs)} changed file(s) with {model} ...", err=True)
    findings = review_diff(client, file_diffs, model)

    report = []
    for finding in findings:
        if no_verify or repo_path is None:
            reason = (
                "verification skipped" if no_verify else "verification requires a local checkout"
            )
            report.append({"finding": finding, "verdict": "PLAUSIBLE", "reason": reason})
            continue

        result = verify_finding(client, finding, repo_path, model)
        if result.verdict == "REFUTED":
            click.echo(
                f"  (dropped, refuted: {finding.file}:{finding.line} -- {finding.summary})",
                err=True,
            )
            continue
        report.append({"finding": finding, "verdict": result.verdict, "reason": result.reason})

    _print_report(report)

    if json_out:
        Path(json_out).write_text(
            json.dumps([_entry_to_dict(e) for e in report], indent=2), encoding="utf-8"
        )
        click.echo(f"Wrote JSON report to {json_out}", err=True)

    if any(e["verdict"] == "CONFIRMED" for e in report):
        sys.exit(1)


def _entry_to_dict(entry: dict) -> dict:
    finding = entry["finding"]
    return {
        "file": finding.file,
        "line": finding.line,
        "summary": finding.summary,
        "failure_scenario": finding.failure_scenario,
        "severity": finding.severity,
        "verdict": entry["verdict"],
        "reason": entry["reason"],
    }


def _print_report(report: list[dict]) -> None:
    if not report:
        click.echo("No findings.")
        return

    order = {"CONFIRMED": 0, "PLAUSIBLE": 1}
    for entry in sorted(report, key=lambda e: order.get(e["verdict"], 2)):
        finding = entry["finding"]
        click.echo("")
        click.echo(f"[{entry['verdict']}] {finding.file}:{finding.line} ({finding.severity})")
        click.echo(f"  {finding.summary}")
        click.echo(f"  Scenario: {finding.failure_scenario}")
        click.echo(f"  ({entry['reason']})")


if __name__ == "__main__":
    main()
