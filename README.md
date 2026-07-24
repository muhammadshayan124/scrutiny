# scrutiny

An AI code-review CLI that tries to *prove* each finding before reporting it, instead of asking the LLM how confident it feels.

```
$ scrutiny review .
Reviewing 1 changed file(s) with claude-opus-4-8 ...
  (dropped, refuted: math_utils.py:4 -- "off-by-one in the loop bound")

[CONFIRMED] math_utils.py:2 (high)
  add() subtracts instead of adding
  Scenario: add(2, 3) should return 5 but returns -1
  (repro genuinely fails as predicted)
```

## The problem with most AI code review

Every AI review tool -- CodeRabbit, GitHub Copilot's PR review, Greptile, and the open-source options (Gito, Alibaba's open-code-review) -- works the same way at its core: an LLM reads a diff and reports what it thinks is wrong. The reported "confidence" is the model's own self-assessment, not evidence.

Real benchmarks show what that costs: Greptile catches 82% of injected bugs but produces enough false positives that [teams report 30-50% of its findings need manual triage](https://dev.to/jovan_chan_9500711396d4e6/greptile-review-2026-82-bug-catch-rate-the-1review-trap-and-who-should-pay-30month-4jao) to tell real issues from noise. CodeRabbit trades the other way -- 44% catch rate, but lower noise. Nobody in this comparison is *verifying* anything; they're all reporting an opinion, just tuned to different points on the same recall/precision tradeoff.

## What scrutiny does differently

For every finding on a Python file, scrutiny asks the LLM to write a second, narrower thing: a concrete pytest test that asserts the *correct* expected behavior for the specific inputs in the described failure scenario, then **actually runs it** against your real code in a subprocess.

- If the repro genuinely fails (the code's real behavior doesn't match the correct expected behavior) → **CONFIRMED**, with the repro attached so you can check it yourself.
- If the repro runs and passes → the finding is **dropped entirely**, not just downgraded. A passing repro is evidence the original finding was a false positive.
- If verification isn't possible (non-Python, the bug needs a live network call/database/real timing, the function can't be isolated) → **PLAUSIBLE**, labeled as unverified, not confirmed.

This means a CONFIRMED verdict comes with a pass/fail result you can inspect, not a probability the model made up. I couldn't find an existing open-source review tool that does this -- see [Related work](#related-work-and-a-real-limitation) below for exactly what I checked before building it.

## Install

```bash
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
# Review uncommitted working-tree changes in the current repo
scrutiny review .

# Review everything on a branch relative to main
scrutiny review . --base main

# Review a GitHub PR (LLM findings only -- see limitation below)
scrutiny review https://github.com/owner/repo/pull/123

# Skip verification entirely, just get raw LLM findings
scrutiny review . --no-verify

# Write the full report as JSON
scrutiny review . --json-out report.json
```

Exit code is `1` if any finding is CONFIRMED (so it can gate CI), `0` otherwise.

## A real safety tradeoff, stated plainly

Verification works by running LLM-generated code against your codebase. It runs in a fresh temp directory with a timeout, but **it is not a sandbox** -- it has the same access to your machine as anything else running in that Python environment. Don't point this at diffs you don't already trust the source of. Read a CONFIRMED repro before acting on it; verification reduces false positives, it does not make the tool trustworthy against adversarial input.

## Related work, and a real limitation

This is a crowded space, and I looked at it before building anything:

- **[CodeRabbit](https://coderabbit.ai), [Greptile](https://greptile.com), GitHub Copilot's PR review** -- commercial, LLM-opinion-based, no execution-based verification step as far as their public benchmarks and docs describe.
- **[Gito](https://github.com/Nayjest/Gito), [Alibaba's open-code-review](https://github.com/alibaba/open-code-review)** -- open-source, hybrid deterministic-rule + LLM-agent pipelines with "high-confidence" framing, but that confidence is still the agent's own assessment, not a pass/fail repro.
- Claude Code itself ships a built-in `/code-review` command with confidence-graded (confirmed/plausible) output. scrutiny's verification step goes one step further than a confidence label: it requires an actual failing repro to earn CONFIRMED, and it's a standalone tool usable outside Claude Code (any repo, any CI, with just an Anthropic API key).

**The real limitation**: verification only works for Python, and only for bugs that reduce to a single-function unit test with no live network/database/timing dependency. Everything else -- other languages, integration-level bugs, concurrency issues -- falls back to PLAUSIBLE, same as a plain LLM review would give you. This isn't a rounding error to gloss over: a large fraction of real bugs (race conditions, cross-service issues) simply aren't verifiable this way, and scrutiny says so honestly in the output rather than quietly downgrading to a vague "medium confidence."

A second, subtler limitation: refuting a finding depends on the LLM writing a *correct* repro. A badly-written repro that doesn't actually exercise the described failure could cause a real bug to be wrongly dropped. This tool is tuned to minimize noise, which trades away some recall -- documented, not hidden.

## PR mode is review-only

Reviewing a GitHub PR URL directly (`scrutiny review <pr-url>`) fetches the diff via `gh pr diff` but has no local checkout to run repros against, so verification is skipped and every finding is reported as PLAUSIBLE. For full verification, clone the PR branch locally (`gh pr checkout <number>`) and run `scrutiny review .` against the checkout instead.

## Tests

23 tests: diff parsing against real `git diff` output (not hand-written diff text), the LLM review pass against a scripted fake client, and -- most importantly -- the verification harness tested by actually running real pytest subprocesses against hand-written "as if the LLM wrote it" repros for all three outcomes (a genuine bug that gets CONFIRMED, a false positive that gets REFUTED, and an unrelated error that stays PLAUSIBLE), plus a full CLI end-to-end test with a real temp git repo.

```bash
pytest -q
ruff check src tests
ruff format --check src tests
```

## What's deliberately out of scope for v1

- **No auto-posting to GitHub.** scrutiny prints/writes a report; it never comments on a PR itself. Posting is a side effect on someone else's repo and belongs behind an explicit, separate step a human triggers.
- **No multi-file / cross-file context.** Each changed file is reviewed independently, the same limitation GitHub Copilot's PR review has. Whole-codebase context (what Greptile's pitch is built on) is real future work, not something this version claims.
- **No non-Python verification.** Documented above, not silently patched over with a weaker check.
