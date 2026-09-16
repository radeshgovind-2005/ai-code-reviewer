import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("cap_diff", ROOT / "scripts" / "cap-diff.py")
cap_diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap_diff)


def file_diff(path, n):
    body = "".join(f"+line {i}\n" for i in range(n))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{n} @@\n{body}"


class CapDiff(unittest.TestCase):
    def test_under_budget_unchanged(self):
        d = file_diff("a.py", 3) + file_diff("b.py", 3)
        out, omitted = cap_diff.cap(d, 10_000)
        self.assertEqual(out, d)
        self.assertEqual(omitted, [])

    def test_drops_whole_files_over_budget(self):
        a, b, c = file_diff("a.py", 3), file_diff("b.py", 500), file_diff("c.py", 3)
        out, omitted = cap_diff.cap(a + b + c, len(a) + len(c) + 5)
        self.assertEqual(omitted, ["b.py"])
        self.assertIn("a.py", out)
        self.assertIn("c.py", out)
        self.assertNotIn("b.py", out)

    def test_first_file_too_big_is_cut_and_listed(self):
        a = file_diff("a.py", 500)
        out, omitted = cap_diff.cap(a, 200)
        self.assertLessEqual(len(out.encode()), 200)
        self.assertTrue(out.startswith("diff --git a/a.py"))
        self.assertEqual(omitted, ["a.py"])


if __name__ == "__main__":
    unittest.main()
