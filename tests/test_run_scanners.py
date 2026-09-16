"""End-to-end tests for scripts/run-scanners.sh with fake scanner binaries
that replay the recorded outputs in tests/fixtures/scanners/."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run-scanners.sh"
FIX = ROOT / "tests" / "fixtures" / "scanners"

# Each fake finds the value after its output flag and copies the fixture
# there (or prints it). FAKE_<TOOL>_RC overrides the exit code.
FAKE = r"""#!/usr/bin/env bash
tool="$(basename "$0")"
echo "$tool $*" >> "$FAKE_DIR/calls.log"
fixture="$FIX_DIR/$FIXNAME.json"
empty_var="FAKE_${FIXNAME^^}_EMPTY"
if [ -n "${!empty_var:-}" ]; then fixture="$FAKE_DIR/empty.json"; echo '[]' > "$fixture"; fi
out=""
prev=""
for a in "$@"; do
  case "$prev" in --report-path|--output-file|--output) out="$a" ;; esac
  prev="$a"
done
if [ -n "$out" ]; then cp "$fixture" "$out"; else cat "$fixture"; fi
rc_var="FAKE_${FIXNAME^^}_RC"
exit "${!rc_var:-0}"
"""

TOOLS = {"gitleaks": "gitleaks", "osv-scanner": "osv", "opengrep": "opengrep",
         "trivy": "trivy", "zizmor": "zizmor", "actionlint": "actionlint"}


class RunScanners(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.bin, self.repo, self.fake = self.tmp / "bin", self.tmp / "repo", self.tmp / "fake"
        for d in (self.bin, self.repo, self.fake):
            d.mkdir()
        for binary, fix in TOOLS.items():
            p = self.bin / binary
            p.write_text(FAKE.replace("${FIXNAME^^}", fix.upper()).replace("$FIXNAME", fix))
            p.chmod(0o755)
        self.rules = self.tmp / "rules"
        (self.rules / "python").mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write("README.md", "hi\n")
        self.base = self.commit("base")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def write(self, rel, content):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        return self.git("rev-parse", "HEAD")

    def playground_pr(self):
        self.write("app/config.py", 'GITHUB_TOKEN = "x"\n')
        self.write("app/handler.py", "".join(f"line{i}\n" for i in range(20)))
        self.write(".github/workflows/pr.yml", "".join(f"l{i}\n" for i in range(12)))
        self.write("Dockerfile", "FROM python:latest\nADD . /app\n")
        self.write("requirements.txt", "requests==2.19.0\n")
        self.commit("pr")

    def run_scan(self, path_prefix=None, **env_extra):
        env = {
            **os.environ,
            "PATH": f"{path_prefix or self.bin}:{os.environ['PATH']}",
            "FAKE_DIR": str(self.fake), "FIX_DIR": str(FIX),
            "BASE_SHA": self.base, "HEAD_SHA": self.git("rev-parse", "HEAD"),
            "REVIEWER_HOME": str(ROOT), "OPENGREP_RULES_DIR": str(self.rules),
            "OUT_DIR": str(self.tmp / "out"), "GITHUB_STEP_SUMMARY": str(self.fake / "summary.md"),
            **env_extra,
        }
        r = subprocess.run(["bash", str(SCRIPT)], cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)
        doc_path = self.tmp / "out" / "scanner-findings.json"
        doc = json.loads(doc_path.read_text()) if doc_path.exists() else None
        return r, doc

    def calls(self):
        log = self.fake / "calls.log"
        return log.read_text() if log.exists() else ""

    def test_playground_blocks(self):
        self.playground_pr()
        r, doc = self.run_scan()
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertGreater(doc["blocking_count"], 0)
        for tool in TOOLS.values():
            self.assertEqual(doc["tools"][tool]["status"], "ok", (tool, doc["tools"]))
        self.assertIn("### Scanners", (self.fake / "summary.md").read_text())
        self.assertTrue((self.tmp / "out" / "scanner-findings.sarif").exists())
        # scanners only get the changed files / commit range
        self.assertIn(f"--log-opts={self.base}..", self.calls())
        self.assertIn("app/handler.py", self.calls())
        self.assertIn(f"--config {self.rules}/python", self.calls())

    def test_docs_only_pr_skips_targeted_scanners(self):
        self.write("docs/guide.md", "hello\n")
        self.commit("pr")
        r, doc = self.run_scan(FAKE_GITLEAKS_EMPTY="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        for tool in ("opengrep", "trivy", "zizmor", "actionlint"):
            self.assertTrue(doc["tools"][tool]["status"].startswith("skipped"), (tool, doc["tools"][tool]))
        self.assertNotIn("opengrep", self.calls())

    def test_missing_tool_is_failed_not_blocking(self):
        self.playground_pr()
        (self.bin / "gitleaks").unlink()
        r, doc = self.run_scan(FAKE_OSV_RC="127")
        self.assertEqual(doc["tools"]["gitleaks"]["status"], "failed: gitleaks not installed")
        self.assertTrue(doc["tools"]["osv"]["status"].startswith("failed"))
        self.assertFalse(any(f["tool"] in ("gitleaks", "osv") for f in doc["findings"]))

    def test_fail_on_error_config(self):
        self.write("review-config.json", json.dumps({"scanners": {"fail_on_error": True, "zizmor": {"enabled": False}}}))
        self.base = self.commit("base config")
        self.write("docs/a.md", "x\n")
        self.commit("pr")
        (self.bin / "gitleaks").unlink()
        r, doc = self.run_scan()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(doc["tools"]["zizmor"]["status"], "disabled")

    def test_scanner_crash_without_output(self):
        self.playground_pr()
        (self.bin / "trivy").write_text("#!/usr/bin/env bash\nexit 3\n")
        r, doc = self.run_scan()
        self.assertTrue(doc["tools"]["trivy"]["status"].startswith("failed: exited 3"))

    def test_gitleaks_config_comes_from_base_branch(self):
        self.write("app/secret.py", "x = 1\n")
        self.write(".gitleaks.toml", "[allowlist]\npaths = ['.*']\n")
        self.write(".gitleaksignore", "everything\n")
        self.commit("pr allowlists itself")
        self.run_scan()
        out = self.tmp / "out"
        self.assertIn(f"--config {out}/gitleaks.toml", self.calls())
        self.assertIn(f"--gitleaks-ignore-path {out}/gitleaksignore", self.calls())
        self.assertEqual((out / "gitleaks.toml").read_text(), "[extend]\nuseDefault = true\n")
        self.assertEqual((out / "gitleaksignore").read_text(), "")

    def test_pr_cannot_disable_scanners_for_itself(self):
        self.playground_pr()
        self.write("review-config.json", json.dumps({"scanners": {"gitleaks": {"enabled": False}}}))
        self.commit("pr disables gitleaks")
        r, doc = self.run_scan()
        self.assertEqual(doc["tools"]["gitleaks"]["status"], "ok")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
