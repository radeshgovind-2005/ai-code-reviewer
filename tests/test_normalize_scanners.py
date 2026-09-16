import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "scanners"
spec = importlib.util.spec_from_file_location("normalize", ROOT / "scripts" / "normalize-scanners.py")
norm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(norm)

# The playground PR the fixtures were recorded from (real tool output, except
# osv.json which follows osv-scanner v2's schema -- its API isn't reachable
# from where the fixtures were recorded).
DIFF = """\
diff --git a/app/config.py b/app/config.py
new file mode 100644
--- /dev/null
+++ b/app/config.py
@@ -0,0 +1,2 @@
+GITHUB_TOKEN = "REDACTED"
+DB_URL = "postgres://..."
diff --git a/app/handler.py b/app/handler.py
new file mode 100644
--- /dev/null
+++ b/app/handler.py
@@ -0,0 +1,15 @@
+import subprocess
+import yaml
+import pickle
+
+
+def run(cmd):
+    return subprocess.call(cmd, shell=True)
+
+
+def load(data):
+    return yaml.load(data)
+
+
+def restore(blob):
+    return pickle.loads(blob)
diff --git a/.github/workflows/pr.yml b/.github/workflows/pr.yml
new file mode 100644
--- /dev/null
+++ b/.github/workflows/pr.yml
@@ -0,0 +1,10 @@
+name: pr
+on: pull_request_target
+jobs:
+  build:
+    runs-on: ubuntu-latest
+    steps:
+      - uses: actions/checkout@v4
+        with:
+          ref: ${{ github.event.pull_request.head.sha }}
+      - run: echo "${{ github.event.pull_request.title }}"
diff --git a/Dockerfile b/Dockerfile
new file mode 100644
--- /dev/null
+++ b/Dockerfile
@@ -0,0 +1,4 @@
+FROM python:latest
+ADD . /app
+RUN pip install -r /app/requirements.txt
+CMD ["python", "/app/handler.py"]
"""
CHANGED = ["app/config.py", "app/handler.py", ".github/workflows/pr.yml", "Dockerfile", "requirements.txt"]


def raw_all():
    return {t: json.loads((FIX / f"{t}.json").read_text()) for t in norm.TOOLS}


class Normalize(unittest.TestCase):
    def run_norm(self, raw=None, statuses=None, config=None, diff=DIFF, changed=CHANGED):
        return norm.normalize(raw if raw is not None else raw_all(), statuses or {}, config or {}, diff, changed)

    def by(self, doc, tool):
        return [f for f in doc["findings"] if f["tool"] == tool]

    def test_every_tool_parses_real_output(self):
        doc = self.run_norm()
        for tool in norm.TOOLS:
            self.assertEqual(doc["tools"][tool]["status"], "ok", tool)
            self.assertGreater(doc["tools"][tool]["count"], 0, tool)
        for f in doc["findings"]:
            self.assertIn(f["severity"], norm.SEVERITY_ORDER)
            self.assertTrue(f["file"])
            self.assertTrue(f["message"])

    def test_default_blocking(self):
        doc = self.run_norm()
        blocking = {(f["tool"], f["rule"]) for f in doc["findings"] if f["blocking"]}
        self.assertIn(("gitleaks", "github-pat"), blocking)
        self.assertIn(("zizmor", "template-injection"), blocking)
        # osv: requests 7.5 (high) and PyYAML 9.8 (critical) block; unscored urllib3 doesn't
        osv = {f["rule"]: f for f in self.by(doc, "osv")}
        self.assertTrue(osv["GHSA-x84v-xcm2-53pg"]["blocking"])
        self.assertEqual(osv["GHSA-8q59-q68h-6hv4"]["severity"], "critical")
        self.assertFalse(osv["PYSEC-2019-133"]["blocking"])
        self.assertIn("Fixed in: 5.4", osv["GHSA-8q59-q68h-6hv4"]["message"])
        # opengrep and actionlint never block by default
        self.assertFalse(any(f["blocking"] for f in self.by(doc, "opengrep") + self.by(doc, "actionlint")))
        # trivy blocks only on critical
        self.assertFalse(any(f["blocking"] for f in self.by(doc, "trivy")))
        self.assertEqual(doc["blocking_count"], len([f for f in doc["findings"] if f["blocking"]]))
        self.assertTrue(doc["findings"][0]["blocking"])  # blocking first

    def test_secret_is_never_printed(self):
        doc = self.run_norm()
        self.assertNotIn("ghp_", json.dumps(doc))

    def test_osv_does_not_block_when_manifest_untouched(self):
        doc = self.run_norm(changed=[c for c in CHANGED if c != "requirements.txt"])
        self.assertTrue(self.by(doc, "osv"))
        self.assertFalse(any(f["blocking"] for f in self.by(doc, "osv")))

    def test_findings_outside_changed_files_are_dropped(self):
        doc = self.run_norm(changed=["requirements.txt"])
        self.assertEqual(self.by(doc, "opengrep"), [])
        self.assertEqual(self.by(doc, "zizmor"), [])

    def test_pre_existing_line_does_not_block(self):
        diff = DIFF.replace('+      - run: echo "${{ github.event.pull_request.title }}"',
                            '       - run: echo "${{ github.event.pull_request.title }}"')
        doc = self.run_norm(diff=diff)
        inj = [f for f in self.by(doc, "zizmor") if f["rule"] == "template-injection"][0]
        self.assertFalse(inj["in_diff"])
        self.assertFalse(inj["blocking"])

    def test_config_block_on_ignore_and_disable(self):
        cfg = {"scanners": {
            "opengrep": {"block_on": "high"},
            "zizmor": {"ignore_rules": ["unpinned-uses"]},
            "trivy": {"enabled": False},
        }}
        doc = self.run_norm(config=cfg)
        self.assertTrue(any(f["blocking"] for f in self.by(doc, "opengrep")))
        self.assertNotIn("unpinned-uses", {f["rule"] for f in self.by(doc, "zizmor")})
        self.assertEqual(doc["tools"]["trivy"]["status"], "disabled")
        self.assertEqual(self.by(doc, "trivy"), [])

    def test_missing_and_failed_tools(self):
        raw = raw_all()
        raw["osv"] = None
        raw["zizmor"] = None
        doc = self.run_norm(raw=raw, statuses={"osv": "failed: timed out after 300s"})
        self.assertEqual(doc["tools"]["osv"]["status"], "failed: timed out after 300s")
        self.assertTrue(doc["tools"]["zizmor"]["status"].startswith("skipped"))

    def test_garbage_output_marks_tool_failed(self):
        raw = raw_all()
        raw["trivy"] = {"Results": "not-a-list"}
        raw["opengrep"] = {"results": [42]}
        doc = self.run_norm(raw=raw)
        self.assertTrue(doc["tools"]["opengrep"]["status"].startswith("failed"))

    def test_markdown_and_sarif(self):
        doc = self.run_norm()
        md = norm.to_markdown(doc)
        self.assertIn("blocking", md)
        self.assertIn("| gitleaks | ok | 1 |", md)
        sarif = norm.to_sarif(doc)
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertEqual(len(sarif["runs"][0]["results"]), len(doc["findings"]))


if __name__ == "__main__":
    unittest.main()
