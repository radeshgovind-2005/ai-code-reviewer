#!/usr/bin/env python3
"""Turns the model's markdown review output into a GitHub PR Review payload
(inline comments anchored to real diff lines + an overall event: APPROVE /
COMMENT / REQUEST_CHANGES), instead of a plain issue comment that GitHub
can't act on -- a plain comment can never block a merge no matter what it
says.

Usage:
  post-review.py --diff DIFF_FILE --commit SHA < review.md > payload.json

Reads the review markdown from stdin. Writes the review JSON payload (for
`gh api .../reviews --input -`) to stdout. Writes a one-line summary
(event + severity counts + how many findings became inline comments vs.
fell back to plain text) to stderr for the caller to fold into logs / the
step summary.
"""
import argparse
import json
import re
import sys

HUNK_RE = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FINDING_RE = re.compile(
    r"^\*\*\[(critical|warning|suggestion)\]\*\*\s+([^\s:][^:]*):(\d+)\s+[—-]\s+(.+)$"
)
SEVERITY_RANK = {"critical": 3, "warning": 2, "suggestion": 1}


def parse_valid_lines(diff_text):
    """file -> set of new-side line numbers actually present in the diff
    (added or context lines) -- the only lines GitHub accepts an inline
    review comment against."""
    valid = {}
    current_file = None
    new_line = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current_file = path[2:] if path.startswith(("a/", "b/")) else path
            if path == "/dev/null":
                current_file = None
            continue
        m = HUNK_RE.match(line)
        if m:
            new_line = int(m.group(1))
            continue
        if new_line is None or current_file is None:
            continue
        if line.startswith("+") or line.startswith(" "):
            valid.setdefault(current_file, set()).add(new_line)
            new_line += 1
        elif line.startswith("-"):
            pass  # old-side only, doesn't advance the new-side counter
    return valid


def parse_review_markdown(text):
    def section(name):
        m = re.search(
            rf"^###\s*{name}\s*$(.*?)(?=^###\s|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        return m.group(1).strip() if m else ""

    summary = section("Summary")
    findings_block = section("Findings")
    verdict = section("Verdict")

    findings = []
    unmatched = []
    for raw_line in findings_block.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        m = FINDING_RE.match(raw_line)
        if m:
            severity, file_path, line_no, desc = m.groups()
            findings.append(
                {
                    "severity": severity,
                    "file": file_path.strip(),
                    "line": int(line_no),
                    "desc": desc.strip(),
                    "raw": raw_line,
                }
            )
        elif raw_line.lower() != "no issues found.":
            # Either a file-only finding (no line number) or malformed --
            # either way, don't drop it, just can't anchor it inline.
            unmatched.append(raw_line)

    return summary, findings, unmatched, verdict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="path to the raw (non-annotated) unified diff")
    parser.add_argument("--commit", required=True, help="head commit SHA the review is against")
    args = parser.parse_args()

    review_markdown = sys.stdin.read()
    with open(args.diff, "r", encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()

    valid_lines = parse_valid_lines(diff_text)
    summary, findings, unmatched, verdict = parse_review_markdown(review_markdown)

    inline_comments = []
    fallback_notes = list(unmatched)
    for f in findings:
        if f["line"] in valid_lines.get(f["file"], set()):
            inline_comments.append(
                {
                    "path": f["file"],
                    "line": f["line"],
                    "side": "RIGHT",
                    "body": f"**[{f['severity']}]** {f['desc']}",
                }
            )
        else:
            # Model cited a line that isn't actually part of the diff --
            # GitHub would reject the whole review over one bad reference,
            # so fall back to plain text instead of silently dropping it.
            fallback_notes.append(f"{f['raw']} (line not resolvable in diff)")

    severities = [f["severity"] for f in findings]
    if any(s == "critical" for s in severities):
        event = "REQUEST_CHANGES"
    elif severities:
        event = "COMMENT"
    else:
        event = "APPROVE"

    body_parts = [f"### Summary\n{summary}" if summary else ""]
    if fallback_notes:
        body_parts.append("### Additional notes\n" + "\n".join(f"- {n}" for n in fallback_notes))
    if verdict:
        body_parts.append(f"### Verdict\n{verdict}")
    body = "\n\n".join(p for p in body_parts if p)

    payload = {"commit_id": args.commit, "body": body, "event": event}
    if inline_comments:
        payload["comments"] = inline_comments

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")

    counts = {k: severities.count(k) for k in SEVERITY_RANK}
    print(
        f"event={event} critical={counts['critical']} warning={counts['warning']} "
        f"suggestion={counts['suggestion']} inline={len(inline_comments)} "
        f"fallback={len(fallback_notes)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
