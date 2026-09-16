"""scripts/review-local.sh: reviews uncommitted changes, posts nothing,
leaves the working tree alone."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

ANSWER = ('```json\n{"summary": "s", "findings": [{"severity": "critical", "file": "a.py", "line": 2, '
          '"description": "Command injection."}], "verdict": "changes_requested"}\n```')
FAKE_OPENCODE = r"""#!/usr/bin/env bash
cat > "$FAKE_DIR/prompt.txt"
jq -cn --arg t "$FAKE_ANSWER" '{type:"text",part:{text:$t}}'
"""


class ReviewLocal(unittest.TestCase):
    def test_reviews_working_tree_without_side_effects(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            (tmp / "bin").mkdir()
            (tmp / "bin" / "opencode").write_text(FAKE_OPENCODE)
            (tmp / "bin" / "opencode").chmod(0o755)
            (tmp / "bin" / "gh").write_text("#!/usr/bin/env bash\ntouch \"$FAKE_DIR/gh-called\"\n")
            (tmp / "bin" / "gh").chmod(0o755)
            repo = tmp / "repo"
            repo.mkdir()

            def git(*a):
                return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout

            git("init", "-q", "-b", "main")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            (repo / "a.py").write_text("x = 1\n")
            git("add", "-A")
            git("commit", "-qm", "base")
            (repo / "a.py").write_text("x = 1\nimport os; os.system(input())\n")  # uncommitted
            env = {**os.environ, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "FAKE_DIR": str(tmp),
                   "LLM_API_KEY": "x", "REVIEW_ENV_PASSTHROUGH": "FAKE_*", "FAKE_ANSWER": ANSWER}
            r = subprocess.run(["bash", str(ROOT / "scripts" / "review-local.sh"), "--base", "main"], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("== REQUEST_CHANGES ==", r.stdout)
            self.assertIn("--- a.py:2", r.stdout)
            self.assertIn("os.system(input())", (tmp / "prompt.txt").read_text())
            self.assertFalse((tmp / "gh-called").exists())
            self.assertEqual(git("status", "--short").strip(), "M a.py")
            self.assertEqual(git("stash", "list").strip(), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
