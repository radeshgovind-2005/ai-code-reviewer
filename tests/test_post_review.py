"""Tests for scripts/post-review.py and its agreement with annotate-diff.py.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
OUTPUTS = FIXTURES / "outputs"
SCRIPT = ROOT / "scripts" / "post-review.py"

spec = importlib.util.spec_from_file_location("post_review", SCRIPT)
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)

DIFF = (FIXTURES / "sample.diff").read_text()
SHA = "deadbeef"


def review(name, no_approve=False):
    text = (OUTPUTS / name).read_text()
    return pr.build_review(text, DIFF, SHA, no_approve)


# name -> (event, source, inline, body_findings, notes)
CASES = {
    "json_critical.txt":            ("REQUEST_CHANGES", "json",     1, 1, 0),
    "json_clean.txt":               ("APPROVE",         "json",     0, 0, 0),
    "json_clean_unfenced.txt":      ("APPROVE",         "json",     0, 0, 0),
    "json_suggestion.txt":          ("COMMENT",         "json",     1, 0, 0),
    "json_bad_line.txt":            ("COMMENT",         "json",     1, 1, 0),
    "json_removed_line.txt":        ("COMMENT",         "json",     1, 1, 0),
    "json_malformed_finding.txt":   ("COMMENT",         "json",     0, 0, 2),
    "json_approve_but_warning.txt": ("COMMENT",         "json",     1, 0, 0),
    "json_verdict_only_blocks.txt": ("REQUEST_CHANGES", "json",     0, 0, 0),
    "json_unknown_verdict.txt":     ("COMMENT",         "json",     0, 0, 0),
    "json_multiple_blocks.txt":     ("REQUEST_CHANGES", "json",     1, 0, 0),
    "json_invalid.txt":             ("COMMENT",         "unparsed", 0, 0, 0),
    "md_pr2_regression.txt":        ("REQUEST_CHANGES", "markdown", 2, 1, 0),
    "md_clean.txt":                 ("COMMENT",         "markdown", 0, 0, 0),
    "prose.txt":                    ("COMMENT",         "unparsed", 0, 0, 0),
    "empty.txt":                    ("COMMENT",         "unparsed", 0, 0, 0),
    "injection.txt":                ("COMMENT",         "json",     1, 0, 0),
}


class FixtureCases(unittest.TestCase):
    def test_every_fixture_has_a_case(self):
        on_disk = {p.name for p in OUTPUTS.iterdir()}
        self.assertEqual(on_disk, set(CASES), "add new fixtures to CASES")

    def test_cases(self):
        for name, (event, source, inline, body, notes) in CASES.items():
            with self.subTest(fixture=name):
                payload, stats = review(name)
                self.assertEqual(stats["event"], event)
                self.assertEqual(payload["event"], event)
                self.assertEqual(stats["source"], source)
                self.assertEqual(stats["inline"], inline)
                self.assertEqual(stats["body"], body)
                self.assertEqual(stats["notes"], notes)
                self.assertEqual(payload["commit_id"], SHA)
                self.assertEqual(len(payload.get("comments", [])), inline)
                self.assertTrue(payload["body"].strip())

    def test_never_approves_unless_json_clean(self):
        for name in CASES:
            with self.subTest(fixture=name):
                _, stats = review(name)
                if stats["event"] == "APPROVE":
                    self.assertEqual(stats["source"], "json")
                    self.assertEqual(stats["verdict"], "approve")
                    self.assertEqual(stats["critical"] + stats["warning"] + stats["suggestion"], 0)
                    self.assertEqual(stats["notes"], 0)

    def test_no_approve_flag(self):
        for name in CASES:
            with self.subTest(fixture=name):
                payload, _ = review(name, no_approve=True)
                self.assertNotEqual(payload["event"], "APPROVE")


class Details(unittest.TestCase):
    def test_inline_comment_shape(self):
        payload, _ = review("json_critical.txt")
        self.assertEqual(payload["comments"], [{
            "path": "auth/session.js",
            "line": 2,
            "side": "RIGHT",
            "body": "**[critical]** Math.random() is not cryptographically secure. Use crypto.randomBytes(32).",
        }])
        self.assertIn("`auth/session.js` — Sessions never expire.", payload["body"])
        self.assertIn("### Verdict\nChanges requested", payload["body"])

    def test_path_prefixes_and_string_lines_normalized(self):
        payload, _ = review("json_bad_line.txt")
        self.assertEqual(payload["comments"][0]["path"], "src/app.js")
        self.assertEqual(payload["comments"][0]["line"], 43)
        self.assertIn("_(line not in diff)_", payload["body"])

    def test_multiple_blocks_uses_last(self):
        payload, _ = review("json_multiple_blocks.txt")
        self.assertIn("real", payload["body"])
        self.assertNotIn("example", payload["body"])

    def test_pr2_regression_no_double_bullets(self):
        payload, _ = review("md_pr2_regression.txt")
        self.assertNotIn("- -", payload["body"])
        self.assertIn("approval disabled", payload["body"])

    def test_unparsed_includes_raw_output(self):
        payload, _ = review("prose.txt")
        self.assertIn("Looks great to me", payload["body"])

    def test_body_is_valid_for_github(self):
        # GitHub rejects empty bodies on REQUEST_CHANGES / COMMENT.
        for name in CASES:
            with self.subTest(fixture=name):
                payload, _ = review(name)
                json.dumps(payload)
                self.assertGreater(len(payload["body"]), 0)


class DiffParsing(unittest.TestCase):
    def test_valid_lines(self):
        valid = pr.parse_valid_lines(DIFF)
        self.assertEqual(valid["auth/session.js"], {1, 2, 3, 4, 5})
        self.assertEqual(valid["src/app.js"], {10, 11, 12, 13, 41, 42, 43})
        self.assertNotIn("old.txt", valid)

    def test_annotate_diff_agrees_with_parser(self):
        """Every line number the model is shown must be one GitHub accepts."""
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "annotate-diff.py")],
            input=DIFF, capture_output=True, text=True, check=True,
        ).stdout
        shown = {}
        current = None
        for line in out.splitlines():
            if line.startswith("+++ "):
                path = line[4:]
                current = None if path == "/dev/null" else path[2:]
                continue
            head = line[:5].strip()
            if current and head.isdigit():
                shown.setdefault(current, set()).add(int(head))
        self.assertEqual(shown, pr.parse_valid_lines(DIFF))


class CLI(unittest.TestCase):
    def test_cli_roundtrip(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--diff", str(FIXTURES / "sample.diff"), "--commit", SHA],
            input=(OUTPUTS / "json_critical.txt").read_text(),
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertIn("event=REQUEST_CHANGES", proc.stderr)
        self.assertIn("source=json", proc.stderr)


if __name__ == "__main__":
    unittest.main()
