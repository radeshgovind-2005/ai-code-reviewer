"""Runs scripts/run-scanners.sh with the REAL pinned scanners against the
seeded playground in examples/playground. Skipped unless
SCANNERS_INTEGRATION=1 (CI installs the tools with install-scanners.sh).

Network note: osv-scanner needs api.osv.dev; if it's unreachable the tool is
reported as failed and the test only checks the offline scanners.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAYGROUND = ROOT / "examples" / "playground"


@unittest.skipUnless(os.environ.get("SCANNERS_INTEGRATION") == "1", "set SCANNERS_INTEGRATION=1")
class ScannersIntegration(unittest.TestCase):
    def test_playground_is_caught(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            repo = tmp / "repo"
            repo.mkdir()

            def git(*a):
                return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            (repo / "README.md").write_text("base\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            shutil.copytree(PLAYGROUND, repo, dirs_exist_ok=True)
            # seeded files are stored with a .seed suffix so this repo's own
            # scanners/workflows don't pick them up
            for p in list(repo.rglob("*.seed")):
                p.rename(p.with_suffix(""))
            git("add", "-A")
            git("commit", "-qm", "seeded pr")
            env = {**os.environ, "BASE_SHA": base, "HEAD_SHA": git("rev-parse", "HEAD"),
                   "REVIEWER_HOME": str(ROOT), "OUT_DIR": str(tmp / "out")}
            r = subprocess.run(["bash", str(ROOT / "scripts" / "run-scanners.sh")], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=900)
            doc = json.loads((tmp / "out" / "scanner-findings.json").read_text())
            print(r.stdout[-4000:])
            self.assertEqual(r.returncode, 1, r.stderr[-2000:])
            rules = {(f["tool"], f["rule"]) for f in doc["findings"]}
            tools = {t for t, _ in rules}
            for tool in ("gitleaks", "opengrep", "trivy", "zizmor", "actionlint"):
                self.assertIn(tool, tools, doc["tools"])
            self.assertIn(("zizmor", "template-injection"), rules)
            if doc["tools"]["osv"]["status"] == "ok":
                self.assertIn("osv", tools)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
