"""Unit tests for scripts/reviewer/: paths, standards, runner event parsing,
retry/fallback policy, merging."""
import json
import os
import pathlib
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from reviewer import opencode_runner as runner  # noqa: E402
from reviewer import paths, pipeline, standards  # noqa: E402


class Paths(unittest.TestCase):
    def test_patterns(self):
        m = paths.matches
        self.assertTrue(m("src/auth/x.py", "auth/"))
        self.assertTrue(m("auth/x.py", "auth/"))
        self.assertFalse(m("src/author.py", "auth/"))
        self.assertTrue(m("deploy/Dockerfile", "Dockerfile"))
        self.assertTrue(m("x.py", "*.py"))
        self.assertTrue(m("a/b/x.py", "*.py"))
        self.assertTrue(m("x.py", "**/*.py"))
        self.assertTrue(m(".github/workflows/a.yml", ".github/workflows/"))
        self.assertTrue(m("./.github/workflows/a.yml", ".github/workflows/"))
        self.assertTrue(m("anything", "**"))
        self.assertTrue(m("docs/guide/intro.md", "docs/"))
        self.assertFalse(m("src/app.js", "*.md"))


class Standards(unittest.TestCase):
    def test_front_matter_and_enforcement(self):
        text = textwrap.dedent("""\
            ---
            id: py-no-bare-except   # comment
            title: "Don't swallow"
            level: must
            status: enforced
            paths: [*.py, 'lib/']
            ---
            Body here.
        """)
        meta, body = standards.parse_front_matter(text)
        std = standards.normalize(meta, body, "x.md")
        self.assertEqual(std["id"], "py-no-bare-except")
        self.assertEqual(std["level"], "MUST")
        self.assertEqual(std["paths"], ["*.py", "lib/"])
        self.assertEqual(standards.applicable([std], ["a/b.py"]), [std])
        self.assertEqual(standards.applicable([std], ["a/b.js"]), [])
        should = {**std, "id": "s", "level": "SHOULD"}
        approved = {**std, "id": "a", "status": "approved"}
        findings = [{"severity": "suggestion", "rule_id": "py-no-bare-except", "description": "d"},
                    {"severity": "critical", "rule_id": "s", "description": "d"},
                    {"severity": "critical", "rule_id": "a", "description": "d"},
                    {"severity": "critical", "rule_id": "unknown", "description": "d"}]
        standards.enforce(findings, [std, should, approved])
        self.assertEqual([f["severity"] for f in findings], ["critical", "warning", "warning", "critical"])
        self.assertNotIn("rule_id", findings[3])

    def test_missing_id_is_ignored(self):
        self.assertIsNone(standards.normalize({"level": "MUST"}, "", "x.md"))
        self.assertEqual(standards.parse_front_matter("no front matter")[0], None)


EVENTS_OK = "\n".join(json.dumps(e) for e in [
    {"type": "step_start", "part": {}},
    {"type": "text", "part": {"text": "Let me look at the caller."}},
    {"type": "tool_use", "part": {"tool": "read"}},
    {"type": "step_finish", "part": {"reason": "tool-calls", "tokens": {"input": 10, "output": 5, "cache": {"read": 100, "write": 7}}, "cost": 0.01}},
    {"type": "step_start", "part": {}},
    {"type": "text", "part": {"text": "```json\n{\"findings\": []}\n```"}},
    {"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 20, "output": 30, "reasoning": 4,
     "cache": {"read": 200, "write": 0}}, "cost": 0.02}},
])


class Events(unittest.TestCase):
    def test_parse_multi_step(self):
        ev = runner.parse_events(EVENTS_OK)
        self.assertEqual(ev["text"], "```json\n{\"findings\": []}\n```")
        self.assertEqual(ev["tokens"], {"input": 30, "output": 35, "reasoning": 4, "cache_read": 300, "cache_write": 7})
        self.assertAlmostEqual(ev["cost"], 0.03)
        self.assertEqual(ev["finish"], "stop")

    def test_parse_error_and_garbage(self):
        ev = runner.parse_events('garbage\n{"type":"error","error":{"name":"APIError","data":{"message":"Overloaded","statusCode":529,"isRetryable":true}}}')
        self.assertEqual(ev["http_status"], 529)
        self.assertEqual(ev["error"], "Overloaded")
        self.assertEqual(ev["text"], "")

    def test_classify(self):
        self.assertEqual(runner.classify("invalid x-api-key", 401, False), "fatal")
        self.assertEqual(runner.classify("prompt is too long: 250000 tokens", 400, False), "fatal")
        self.assertEqual(runner.classify("model: claude-nope not found", 404, False), "fallback")
        self.assertEqual(runner.classify("Overloaded", 529, True), "retry")
        self.assertEqual(runner.classify("rate limit exceeded", 429, False), "retry")
        self.assertEqual(runner.classify("opencode exited 1: boom", None, None), "retry")

    def test_child_env_drops_github_token(self):
        old = dict(os.environ)
        try:
            os.environ.update({"GITHUB_TOKEN": "x", "GH_TOKEN": "y", "LLM_API_KEY": "z", "ANTHROPIC_API_KEY": "k",
                               "FAKE_THING": "1", "RANDOM_VAR": "r"})
            env = runner.child_env(("FAKE_*",))
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("LLM_API_KEY", env)
            self.assertNotIn("RANDOM_VAR", env)
            self.assertEqual(env["ANTHROPIC_API_KEY"], "k")
            self.assertEqual(env["FAKE_THING"], "1")
            self.assertEqual(env["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
        finally:
            os.environ.clear()
            os.environ.update(old)


FAKE = r"""#!/usr/bin/env bash
cat > /dev/null
model=""; prev=""
for a in "$@"; do [ "$prev" = "-m" ] && model="$a"; prev="$a"; done
echo "$model" >> "$FAKE_LOG"
n=$(grep -c . "$FAKE_LOG")
script="${FAKE_SCRIPT:-ok}"
step=$(echo "$script" | cut -d, -f"$n")
[ -z "$step" ] && step=ok
case "$step" in
  ok) echo '{"type":"text","part":{"text":"answer"}}'; echo '{"type":"step_finish","part":{"reason":"stop","tokens":{"input":1}}}' ;;
  overloaded) echo '{"type":"error","error":{"data":{"message":"Overloaded","statusCode":529,"isRetryable":true}}}' ;;
  notfound) echo '{"type":"error","error":{"data":{"message":"model not found","statusCode":404}}}' ;;
  auth) echo '{"type":"error","error":{"data":{"message":"invalid x-api-key","statusCode":401}}}' ;;
  length) echo '{"type":"text","part":{"text":"cut"}}'; echo '{"type":"step_finish","part":{"reason":"length"}}' ;;
  silent) sleep 5 ;;
esac
"""


class Call(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        (self.tmp / "opencode").write_text(FAKE)
        (self.tmp / "opencode").chmod(0o755)
        self.log = self.tmp / "log"
        self.log.write_text("")
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.tmp}:{self.old_path}"
        os.environ["FAKE_LOG"] = str(self.log)

    def tearDown(self):
        os.environ["PATH"] = self.old_path
        os.environ.pop("FAKE_SCRIPT", None)

    def call(self, script, models=("m1", "m2"), **kw):
        os.environ["FAKE_SCRIPT"] = script
        kw.setdefault("timeout", 20)
        return runner.call("prompt", list(models), label="t", backoff=0, heartbeat=0,
                           passthrough=("FAKE_*",), log=open(os.devnull, "w"), **kw)

    def models_called(self):
        return self.log.read_text().split()

    def test_ok_first_try(self):
        r = self.call("ok")
        self.assertTrue(r.ok)
        self.assertEqual((r.text, r.model), ("answer", "m1"))
        self.assertEqual(r.tokens["input"], 1)

    def test_retry_then_success(self):
        r = self.call("overloaded,ok")
        self.assertTrue(r.ok)
        self.assertEqual(self.models_called(), ["m1", "m1"])

    def test_retries_exhausted_then_fallback(self):
        r = self.call("overloaded,overloaded,ok")
        self.assertTrue(r.ok)
        self.assertEqual(self.models_called(), ["m1", "m1", "m2"])
        self.assertEqual(r.model, "m2")

    def test_not_found_falls_back_immediately(self):
        r = self.call("notfound,ok")
        self.assertEqual(self.models_called(), ["m1", "m2"])
        self.assertTrue(r.ok)

    def test_auth_is_fatal(self):
        r = self.call("auth,ok")
        self.assertFalse(r.ok)
        self.assertEqual(self.models_called(), ["m1"])
        self.assertIn("invalid x-api-key", r.error)

    def test_truncated_retried_once_same_model(self):
        r = self.call("length,ok")
        self.assertTrue(r.ok)
        self.assertEqual(self.models_called(), ["m1", "m1"])

    def test_inactivity_kills_and_falls_back(self):
        r = self.call("silent,ok", inactivity=1)
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts[0].status, "inactive")
        self.assertEqual(r.model, "m2")


class Merge(unittest.TestCase):
    def test_dedupe_and_verdict(self):
        merged = pipeline.deterministic_merge({
            "security": [{"severity": "warning", "file": "a.py", "line": 3, "description": "SQL injection via name param"}],
            "correctness": [{"severity": "critical", "file": "a.py", "line": 3, "description": "SQL injection via the name param"},
                            {"severity": "suggestion", "file": "b.py", "line": None, "description": "Other"}],
        })
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["severity"], "critical")
        self.assertEqual(merged[0]["agent"], "security,correctness")
        self.assertEqual(pipeline.verdict_for(merged), "changes_requested")
        self.assertEqual(pipeline.verdict_for([]), "approve")

    def test_select_agents(self):
        cfg = {"agents": {
            "security": {"tiers": ["trivial", "lite", "full"], "paths": ["**"]},
            "docs": {"tiers": ["lite", "full"], "paths": ["*.md"]},
            "standards": {"tiers": ["lite"], "requires_standards": True},
            "off": {"enabled": False},
        }}
        self.assertEqual(pipeline.select_agents(cfg, "trivial", ["a.md"], True), ["security"])
        self.assertEqual(pipeline.select_agents(cfg, "lite", ["a.py"], False), ["security"])
        self.assertEqual(pipeline.select_agents(cfg, "lite", ["a.md"], True), ["security", "docs", "standards"])


if __name__ == "__main__":
    unittest.main()
