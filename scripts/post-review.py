#!/usr/bin/env python3
"""Turns the model's markdown review output into a GitHub PR Review payload
(inline comments anchored to real diff lines + an overall event: APPROVE /
COMMENT / REQUEST_CHANGES), instead of a plain issue comment that GitHub
can't act on -- a plain comment can never block a merge no matter what it
says.

Usage:
  post-review.py --diff DIFF_FILE --commit SHA [--no-approve] < review.md > payload.json

Reads the review markdown from stdin. Writes the review JSON payload (for
`gh api .../reviews --input -`) to stdout. Writes a one-line summary
(event + severity counts + how many findings became inline comments vs.
fell back to plain text) to stderr for the caller to fold into logs / the
step summary.

Event rules (fail safe -- never approve by accident):
  - any critical finding, or the model's verdict says "changes requested"
      -> REQUEST_CHANGES
  - any other finding, any unparsed note in Findings, a verdict other than a
    plain approve, or output we couldn't parse at all
      -> COMMENT
  - "No issues found." + verdict "approve"  -> APPROVE  (COMMENT if --no-approve)
"""
import argparse
import json
import re
import sys

HUNK_RE = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Tolerates what models actually emit, e.g.:
#   **[critical]** auth/session.js:3 — desc
#   - **[critical]** auth/session.js:3 — desc
#   * **critical** `auth/session.js:3` - desc
#   1. **[warning]** auth/session.js — desc          (file-only, no line)
BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
FINDING_RE = re.compile(
    r"^\*\*\s*\[?\s*(critical|warning|suggestion)\s*\]?\s*\*\*:?\s+"
    r"`?([^\s`:]+?)(?::(\d+)(?:-\d+)?)?`?"
    r"\s+[—–-]+\s+(.+)$",
    re.IGNORECASE,
)
SEVERITY_RANK = {"critical": 3, "warning": 2, "suggestion": 1}
NO_ISSUES_RE = re.compile(r"^_?no issues found\.?_?$", re.IGNORECASE)


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
            current_file = None if path == "/dev/null" else (
                path[2:] if path.startswith(("a/", "b/")) else path
            )
            new_line = None
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
    return valid


def strip_bullet(line):
    return BULLET_RE.sub("", line, count=1).strip()


def parse_review_markdown(text):
    def section(name):
        m = re.search(
            rf"^#{{1,4}}\s*{name}\s*:?\s*$(.*?)(?=^#{{1,4}}\s|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        return m.group(1).strip() if m else None

    summary = section("Summary")
    findings_block = section("Findings")
    verdict = section("Verdict")

    findings = []
    unmatched = []
    for raw_line in (findings_block or "").splitlines():
        line = strip_bullet(raw_line.strip())
        if not line:
            continue
        if NO_ISSUES_RE.match(line):
            continue
        m = FINDING_RE.match(line)
        if m:
            severity, file_path, line_no, desc = m.groups()
            findings.append(
                {
                    "severity": severity.lower(),
                    "file": file_path[2:] if file_path.startswith("./") else file_path,
                    "line": int(line_no) if line_no else None,
                    "desc": desc.strip(),
                    "raw": line,
                }
            )
        else:
            unmatched.append(line)

    return summary, findings_block, findings, unmatched, verdict


def classify_verdict(verdict):
    v = (verdict or "").strip().lower()
    if not v:
        return "unknown"
    if "changes requested" in v or "request changes" in v or "request_changes" in v:
        return "changes_requested"
    if "with comments" in v:
        return "approve_with_comments"
    if v.startswith("approve") or v.startswith("approved") or v.startswith("lgtm"):
        return "approve"
    return "unknown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="path to the raw (non-annotated) unified diff")
    parser.add_argument("--commit", required=True, help="head commit SHA the review is against")
    parser.add_argument("--no-approve", action="store_true",
                        help="never emit APPROVE; a clean review becomes COMMENT")
    args = parser.parse_args()

    review_markdown = sys.stdin.read()
    with open(args.diff, "r", encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()

    valid_lines = parse_valid_lines(diff_text)
    summary, findings_block, findings, unmatched, verdict = parse_review_markdown(review_markdown)
    verdict_kind = classify_verdict(verdict)
    parse_ok = findings_block is not None and verdict is not None

    inline_comments = []
    fallback_notes = list(unmatched)
    for f in findings:
        if f["line"] is not None and f["line"] in valid_lines.get(f["file"], set()):
            inline_comments.append(
                {
                    "path": f["file"],
                    "line": f["line"],
                    "side": "RIGHT",
                    "body": f"**[{f['severity']}]** {f['desc']}",
                }
            )
        elif f["line"] is None:
            fallback_notes.append(f["raw"])
        else:
            # Cited line isn't part of the diff -- GitHub would reject the
            # whole review over one bad reference, so keep it as body text.
            fallback_notes.append(f"{f['raw']} _(line not in diff)_")

    severities = [f["severity"] for f in findings]
    if "critical" in severities or verdict_kind == "changes_requested":
        event = "REQUEST_CHANGES"
    elif (
        not parse_ok
        or severities
        or unmatched
        or verdict_kind != "approve"
    ):
        event = "COMMENT"
    else:
        event = "APPROVE"
    if event == "APPROVE" and args.no_approve:
        event = "COMMENT"

    body_parts = []
    if summary:
        body_parts.append(f"### Summary\n{summary}")
    if fallback_notes:
        body_parts.append("### Additional notes\n" + "\n".join(f"- {n}" for n in fallback_notes))
    if verdict:
        body_parts.append(f"### Verdict\n{verdict}")
    if not parse_ok:
        body_parts.append(
            "> ⚠️ The reviewer's output didn't match the expected format, so this "
            "review can't approve. Raw output:\n\n" + review_markdown.strip()
        )
    body = "\n\n".join(body_parts) or "AI review produced no output."

    payload = {"commit_id": args.commit, "body": body, "event": event}
    if inline_comments:
        payload["comments"] = inline_comments

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")

    counts = {k: severities.count(k) for k in SEVERITY_RANK}
    print(
        f"event={event} critical={counts['critical']} warning={counts['warning']} "
        f"suggestion={counts['suggestion']} inline={len(inline_comments)} "
        f"fallback={len(fallback_notes)} verdict={verdict_kind} parsed={str(parse_ok).lower()}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
