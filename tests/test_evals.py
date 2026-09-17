"""Tests the eval harness and the eval cases themselves (without a model):
an oracle runner that answers exactly what each case expects must pass every
case, an empty runner must fail the cases that expect findings, and every
expected line must be an added line of the case's diff."""
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "scripts"))

import run as evals  # noqa: E402
from reviewer.opencode_runner import CallResult  # noqa: E402

CONFIG_PATH = ROOT / "review-config.json"
CONFIG = json.loads(CONFIG_PATH.read_text())
QUIET = open(os.devnull, "w")
CASES = {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "evals" / "cases").glob("*.json"))}


def answer(findings, verdict):
    return CallResult(ok=True, model="fake", text="```json\n" + json.dumps(
        {"summary": "s", "findings": findings, "verdict": verdict}) + "\n```")


def oracle_for(case):
    exp = case["expect"]
    findings = [{"severity": e.get("min_severity", "warning") if e.get("min_severity") != "suggestion" else "warning",
                 "file": e["file"], "line": e.get("line"), "description": f"{e['keywords'][0]} problem"}
                for e in exp.get("must_find", [])]
    if any(f["severity"] == "critical" for f in findings):
        verdict = "changes_requested"
    else:
        verdict = "approve_with_comments" if findings else "approve"
    allowed = exp.get("verdict_in")
    if allowed and verdict not in allowed:
        verdict = allowed[0]
        if verdict == "changes_requested":
            for f in findings:
                f["severity"] = "critical"

    def runner(prompt, models, **kw):
        return answer(findings, verdict)
    return runner


class EvalCases(unittest.TestCase):
    def test_cases_are_well_formed_and_lines_are_added_lines(self):
        self.assertGreaterEqual(len(CASES), 10)
        for name, case in CASES.items():
            with self.subTest(case=name):
                self.assertIn("expect", case)
                import tempfile
                work = tempfile.mkdtemp()
                repo, base, head = evals.build_repo(case, work)
                diff = evals.git(repo, "diff", f"{base}...{head}")
                annotated = subprocess.run([sys.executable, str(ROOT / "scripts" / "annotate-diff.py")], input=diff,
                                           capture_output=True, text=True).stdout
                for e in case["expect"].get("must_find", []):
                    self.assertIn(f"+++ b/{e['file']}", diff)
                    if e.get("line") is not None:
                        self.assertRegex(annotated, rf"(?m)^\s*{e['line']} \+ ", f"{name}: line {e['line']} not added")

    def test_oracle_passes_everything(self):
        results = [evals.run_case(n, c, CONFIG, CONFIG_PATH, runner=oracle_for(c), log=QUIET) for n, c in CASES.items()]
        failed = [(r["case"], r["missed"], r["verdict"], r["fp_findings"]) for r in results if not r["passed"]]
        self.assertEqual(failed, [])
        summary = evals.summarize(results)
        self.assertEqual(summary["recall"], 1.0)
        self.assertIn("passed", evals.to_markdown(summary, results))

    def test_blind_reviewer_fails_cases_with_expectations(self):
        blind = lambda prompt, models, **kw: answer([], "approve")  # noqa: E731
        results = {n: evals.run_case(n, c, CONFIG, CONFIG_PATH, runner=blind, log=QUIET) for n, c in CASES.items()}
        self.assertTrue(results["clean-refactor"]["passed"])
        self.assertFalse(results["sql-injection"]["passed"])
        self.assertEqual(evals.summarize(list(results.values()))["recall"], 0.0)

    def test_noisy_reviewer_fails_clean_case(self):
        noisy = lambda prompt, models, **kw: answer(  # noqa: E731
            [{"severity": "warning", "file": "app/pricing.py", "line": 6, "description": "Consider naming"}], "approve_with_comments")
        r = evals.run_case("clean-refactor", CASES["clean-refactor"], CONFIG, CONFIG_PATH, runner=noisy, log=QUIET)
        self.assertFalse(r["passed"])
        self.assertEqual(r["false_positives"], 1)


if __name__ == "__main__":
    unittest.main()
