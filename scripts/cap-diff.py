#!/usr/bin/env python3
"""Caps a unified diff to a byte budget, keeping whole files.

  cap-diff.py --max-bytes N --omitted OMITTED_FILE < diff > capped.diff

Files are kept in diff order until the next one would exceed the budget; the
rest are dropped and their paths written (one per line) to OMITTED_FILE. If
even the first file is over budget it is cut at a line boundary and listed as
omitted too, so the reviewer never silently sees a partial file as whole.
Exit code is always 0; the caller checks whether OMITTED_FILE is non-empty.
"""
import argparse
import re
import sys

HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")


def split_files(text):
    chunks, cur = [], []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") and cur:
            chunks.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


def path_of(chunk):
    m = HEADER_RE.match(chunk.splitlines()[0]) if chunk else None
    return m.group(2) if m else "(unknown)"


def cap(text, max_bytes):
    kept, omitted, used = [], [], 0
    for chunk in split_files(text):
        size = len(chunk.encode())
        if used + size <= max_bytes:
            kept.append(chunk)
            used += size
            continue
        if not kept:
            # First file alone is too big: keep a prefix of whole lines.
            part = []
            for line in chunk.splitlines(keepends=True):
                n = len(line.encode())
                if used + n > max_bytes:
                    break
                part.append(line)
                used += n
            kept.append("".join(part))
        omitted.append(path_of(chunk))
    return "".join(kept), omitted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-bytes", type=int, required=True)
    ap.add_argument("--omitted", required=True)
    args = ap.parse_args()
    capped, omitted = cap(sys.stdin.read(), args.max_bytes)
    sys.stdout.write(capped)
    with open(args.omitted, "w") as f:
        f.write("".join(p + "\n" for p in omitted))


if __name__ == "__main__":
    main()
