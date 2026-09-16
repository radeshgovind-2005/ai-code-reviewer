#!/usr/bin/env python3
"""Reads a unified diff on stdin, writes it back out with each added/context
line prefixed by its NEW-file line number, so a model reviewing the diff can
cite a line number that we can later verify and use to post a real inline
GitHub PR review comment (see post-review.py). Diff metadata lines pass
through unchanged.
"""
import re
import sys

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def main() -> None:
    new_line = None
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        m = HUNK_RE.match(line)
        if m:
            new_line = int(m.group(2))
            print(line)
            continue
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file", "Binary files")):
            print(line)
            continue
        if new_line is None:
            print(line)
            continue
        if line.startswith("+"):
            print(f"{new_line:>5} + {line[1:]}")
            new_line += 1
        elif line.startswith("-"):
            print(f"    - {line[1:]}")
        elif line.startswith(" "):
            print(f"{new_line:>5}   {line[1:]}")
            new_line += 1
        elif line.startswith("\\"):
            print(line)
        else:
            print(line)


if __name__ == "__main__":
    main()
