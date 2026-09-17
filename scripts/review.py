#!/usr/bin/env python3
"""Runs the multi-agent review and writes its results for run-review.sh.

  review.py --reviewer-home DIR --repo-dir DIR --config CONFIG --annotated ANNOTATED_DIFF
            --changed-files FILE --tier T --reason R [--sensitive] [--base-sha SHA]
            [--omitted FILE] [--scanner-findings FILE] [--previous-threads FILE]
            [--free-tier] --out-dir DIR

Writes to OUT_DIR:
  final-output.txt   the final review as a ```json block (post-review.py input)
  decision.json      agents run/failed, no_approve_reasons, resolved_previous, ...
  metrics.json       per-agent model, attempts, duration, tokens, cost + totals
  metrics.md         one-line footer for the review body
  prompt-<agent>.md  the exact prompts (for debugging)

Env: PR_TITLE, PR_BODY (untrusted), MODEL_TIMEOUT (seconds per call).
Exit: 0 review produced · 3 no reviewer completed · other = crash.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reviewer import pipeline  # noqa: E402


def read_lines(path):
    if not path or not os.path.exists(path):
        return []
    with open(path) as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def read_json(path, default):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return default
    try:
        with open(path) as fh:
            return json.load(fh)
    except ValueError:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewer-home", required=True)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--annotated", required=True)
    ap.add_argument("--changed-files", required=True)
    ap.add_argument("--tier", required=True)
    ap.add_argument("--reason", default="")
    ap.add_argument("--sensitive", action="store_true")
    ap.add_argument("--base-sha", default="")
    ap.add_argument("--omitted")
    ap.add_argument("--scanner-findings")
    ap.add_argument("--previous-threads")
    ap.add_argument("--free-tier", action="store_true")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    with open(args.config) as fh:
        config = json.load(fh)
    with open(args.annotated, encoding="utf-8", errors="replace") as fh:
        annotated = fh.read()
    scanner_doc = read_json(args.scanner_findings, {})
    timeout = os.environ.get("MODEL_TIMEOUT")

    inp = pipeline.ReviewInputs(
        reviewer_home=args.reviewer_home,
        repo_dir=args.repo_dir,
        config=config,
        annotated_diff=annotated,
        changed_files=read_lines(args.changed_files),
        tier=args.tier,
        reason=args.reason,
        sensitive=args.sensitive,
        base_sha=args.base_sha,
        omitted_files=read_lines(args.omitted),
        scanner_findings=scanner_doc.get("findings", []) if isinstance(scanner_doc, dict) else [],
        previous_threads=read_json(args.previous_threads, []),
        pr_title=os.environ.get("PR_TITLE", ""),
        pr_body=os.environ.get("PR_BODY", ""),
        model_timeout=int(timeout) if timeout and timeout.isdigit() else None,
        free_tier=args.free_tier,
        out_dir=args.out_dir,
    )
    outcome = pipeline.run_review(inp)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(outcome.metrics, indent=2))
    (out / "decision.json").write_text(json.dumps(outcome.decision, indent=2))
    (out / "metrics.md").write_text(pipeline.footer(outcome.metrics, outcome.decision) + "\n")
    if not outcome.ok:
        failures = "; ".join(f"{a['name']}: {a['error']}" for a in outcome.metrics.get("agents", []) if a["status"] != "ok")
        print(f"error: {outcome.decision.get('error')}: {failures}", file=sys.stderr)
        (out / "failure.txt").write_text(f"{outcome.decision.get('error')}. {failures}"[:1500])
        return 3
    (out / "final-output.txt").write_text("```json\n" + json.dumps(outcome.final, indent=2) + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
