#!/usr/bin/env python3
"""Eval harness: runs the real review pipeline on seeded PRs and scores it.

  python3 evals/run.py [--cases evals/cases] [--only name,name] [--config review-config.json]
                       [--out evals/results] [--repeat N]

Needs `opencode` on PATH and a provider key (e.g. ANTHROPIC_API_KEY or
LLM_API_KEY, exported the same way run-review.sh does). Nothing is posted.

Each case (evals/cases/<name>.json) has `before` / `after` file contents and:
  tier: optional, pins the tier (trivial|lite|full) to exercise specific reviewers
  expect.must_find: [{file, line?, line_tolerance? (default 2), keywords[], min_severity}]
  expect.verdict_in: allowed final verdicts (optional)
  expect.max_false_positives: max findings (severity >= warning) that match
                              no must_find entry (optional, default unlimited)

Scores per case: recall (must_find hit), false positives, verdict ok, time,
tokens, cost. Totals go to <out>/results.json and <out>/results.md -- paste
the summary row into the roadmap's Metrics table.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from reviewer import opencode_runner, pipeline  # noqa: E402

SEV_RANK = {"suggestion": 0, "warning": 1, "critical": 2}
PROVIDER_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY",
                 "groq": "GROQ_API_KEY", "google": "GOOGLE_GENERATIVE_AI_API_KEY"}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def build_repo(case, workdir):
    repo = Path(workdir) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "eval@example.com")
    git(repo, "config", "user.name", "eval")
    for path, content in {"README.md": "eval repo\n", **case.get("before", {})}.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD").strip()
    for path, content in case.get("after", {}).items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "pr")
    head = git(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


def tier_for(repo, base, head, config_path):
    env = {**os.environ, "BASE_SHA": base, "HEAD_SHA": head, "REVIEWER_HOME": str(ROOT), "CONFIG_FILE": str(config_path)}
    out = subprocess.run(["bash", str(ROOT / "scripts" / "tier-pr.sh")], cwd=repo, env=env, check=True,
                         capture_output=True, text=True).stdout
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return kv["TIER"], kv.get("REASON", ""), kv.get("SENSITIVE") == "true"


def matches(finding, exp):
    if finding.get("file") != exp["file"]:
        return False
    if SEV_RANK.get(finding.get("severity"), -1) < SEV_RANK[exp.get("min_severity", "suggestion")]:
        return False
    if exp.get("line") is not None and finding.get("line") is not None:
        if abs(finding["line"] - exp["line"]) > exp.get("line_tolerance", 2):
            return False
    text = str(finding.get("description", "")).lower()
    return any(k.lower() in text for k in exp.get("keywords", [])) if exp.get("keywords") else True


def score(case, final):
    expect = case.get("expect", {})
    findings = final.get("findings", []) if final else []
    must = expect.get("must_find", [])
    hits = [any(matches(f, e) for f in findings) for e in must]
    fps = [f for f in findings if SEV_RANK.get(f.get("severity"), 0) >= 1 and not any(matches(f, e) for e in must)]
    verdict_ok = (not expect.get("verdict_in")) or (final or {}).get("verdict") in expect["verdict_in"]
    fp_ok = expect.get("max_false_positives") is None or len(fps) <= expect["max_false_positives"]
    return {
        "must_find": len(must), "found": sum(hits), "recall": (sum(hits) / len(must)) if must else None,
        "false_positives": len(fps), "fp_ok": fp_ok, "verdict": (final or {}).get("verdict"), "verdict_ok": verdict_ok,
        "passed": all(hits) and verdict_ok and fp_ok,
        "missed": [e for e, h in zip(must, hits) if not h],
        "fp_findings": [{k: f.get(k) for k in ("severity", "file", "line", "description", "agent")} for f in fps],
    }


def run_case(name, case, config, config_path, runner=opencode_runner.call, keep=False, log=sys.stderr):
    work = tempfile.mkdtemp(prefix=f"eval-{name}-")
    try:
        repo, base, head = build_repo(case, work)
        tier, reason, sensitive = tier_for(repo, base, head, config_path)
        if case.get("tier"):  # a case can pin the tier to exercise specific reviewers
            tier, reason = case["tier"], f"pinned by eval case (computed: {tier})"
        diff = git(repo, "diff", f"{base}...{head}")
        annotated = subprocess.run([sys.executable, str(ROOT / "scripts" / "annotate-diff.py")], input=diff,
                                   capture_output=True, text=True, check=True).stdout
        changed = git(repo, "diff", "--name-only", f"{base}...{head}").split()
        inp = pipeline.ReviewInputs(reviewer_home=str(ROOT), repo_dir=str(repo), config=config, annotated_diff=annotated,
                                    changed_files=changed, tier=tier, reason=reason, sensitive=sensitive, base_sha=base,
                                    out_dir=str(Path(work) / "out"), log=log)
        started = time.monotonic()
        outcome = pipeline.run_review(inp, runner=runner)
        result = score(case, outcome.final if outcome.ok else None)
        result.update({"case": name, "tier": tier, "ok": outcome.ok, "duration_s": round(time.monotonic() - started, 1),
                       "cost_usd": outcome.metrics.get("totals", {}).get("cost_usd", 0),
                       "tokens": outcome.metrics.get("totals", {}).get("tokens", {}),
                       "agents": outcome.decision.get("agents"), "failed_agents": outcome.decision.get("failed_agents"),
                       "final": outcome.final})
        return result
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def summarize(results):
    with_must = [r for r in results if r["must_find"]]
    durations = sorted(r["duration_s"] for r in results)

    def pct(p):
        return durations[min(len(durations) - 1, int(round(p * (len(durations) - 1))))] if durations else 0

    return {
        "cases": len(results),
        "passed": sum(r["passed"] for r in results),
        "recall": round(sum(r["found"] for r in with_must) / max(1, sum(r["must_find"] for r in with_must)), 3),
        "false_positives_per_case": round(sum(r["false_positives"] for r in results) / max(1, len(results)), 2),
        "verdict_accuracy": round(sum(r["verdict_ok"] for r in results) / max(1, len(results)), 3),
        "p50_s": pct(0.5), "p95_s": pct(0.95),
        "avg_cost_usd": round(sum(r["cost_usd"] for r in results) / max(1, len(results)), 4),
        "failed_runs": sum(not r["ok"] for r in results),
    }


def to_markdown(summary, results):
    lines = ["| case | tier | passed | found | FPs | verdict | time | cost |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['case']} | {r['tier']} | {'✅' if r['passed'] else '❌'} | {r['found']}/{r['must_find']} | "
                     f"{r['false_positives']} | {r['verdict']}{'' if r['verdict_ok'] else ' ⚠️'} | {r['duration_s']}s | ${r['cost_usd']} |")
    s = summary
    lines += ["", f"**{s['passed']}/{s['cases']} passed** · recall {s['recall']} · FP/case {s['false_positives_per_case']} · "
              f"verdict acc. {s['verdict_accuracy']} · p50 {s['p50_s']}s · p95 {s['p95_s']}s · avg ${s['avg_cost_usd']}"]
    for r in results:
        for m in r["missed"]:
            lines.append(f"- {r['case']}: missed {m['file']}:{m.get('line')} ({', '.join(m.get('keywords', []))})")
        for f in r["fp_findings"]:
            lines.append(f"- {r['case']}: false positive [{f['severity']}] {f['file']}:{f['line']} {str(f['description'])[:120]}")
    return "\n".join(lines) + "\n"


def export_provider_keys(config):
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return
    models = [a.get("model", "") for a in config.get("agents", {}).values()] + [config.get("coordinator", {}).get("model", "")]
    for m in models:
        var = PROVIDER_VARS.get(m.split("/")[0])
        if var and not os.environ.get(var):
            os.environ[var] = key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases"))
    ap.add_argument("--only", default="")
    ap.add_argument("--config", default=str(ROOT / "review-config.json"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "results"))
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--keep", action="store_true", help="keep temp repos and prompts")
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text())
    export_provider_keys(config)
    only = {x for x in args.only.split(",") if x}
    results = []
    for path in sorted(Path(args.cases).glob("*.json")):
        if only and path.stem not in only:
            continue
        case = json.loads(path.read_text())
        for i in range(args.repeat):
            name = path.stem if args.repeat == 1 else f"{path.stem}#{i + 1}"
            print(f"== {name}", file=sys.stderr, flush=True)
            results.append(run_case(name, case, config, args.config, keep=args.keep))
    summary = summarize(results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    md = to_markdown(summary, results)
    (out / "results.md").write_text(md)
    print(md)
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    sys.exit(main())
