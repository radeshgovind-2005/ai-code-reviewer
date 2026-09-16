#!/usr/bin/env python3
"""Turns the model's review output into a GitHub PR Review payload (inline
comments anchored to real diff lines + an overall event: APPROVE / COMMENT /
REQUEST_CHANGES).

Usage:
  post-review.py --diff DIFF_FILE --commit SHA [--no-approve] < model_output > payload.json

Input contract (see agents/reviewer.md): one JSON object in a ```json fence:
  {"summary": str, "findings": [{"severity", "file", "line", "description"}],
   "verdict": "approve" | "approve_with_comments" | "changes_requested"}

If the JSON can't be found/parsed, falls back to the legacy markdown format
(### Summary / ### Findings / ### Verdict). A fallback parse can never APPROVE.

Writes the payload JSON to stdout and a one-line stats summary to stderr.

Event rules (fail safe -- never approve by accident):
  - any critical finding, or verdict changes_requested     -> REQUEST_CHANGES
  - any other finding, any malformed/unparsed entry, a verdict other than
    approve, markdown fallback, or unparseable output       -> COMMENT
  - JSON output, no findings, verdict approve               -> APPROVE
    (COMMENT instead when --no-approve)
"""
import argparse
import json
import re
import sys

SEVERITIES = ("critical", "warning", "suggestion")
VERDICTS = ("approve", "approve_with_comments", "changes_requested")

HUNK_RE = re.compile(r"^@@ -(?:\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)

# Legacy markdown format.
BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
MD_FINDING_RE = re.compile(
    r"^\*\*\s*\[?\s*(critical|warning|suggestion)\s*\]?\s*\*\*:?\s+"
    r"`?([^\s`:]+?)(?::(\d+)(?:-\d+)?)?`?"
    r"\s+[—–-]+\s+(.+)$",
    re.IGNORECASE,
)
NO_ISSUES_RE = re.compile(r"^_?no issues found\.?_?$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------
def parse_valid_lines(diff_text):
    """file -> set of new-side line numbers present in the diff (added or
    context lines) -- the only lines GitHub accepts inline comments on."""
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


def normalize_path(path):
    path = str(path).strip().strip("`")
    for prefix in ("./", "a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


# --------------------------------------------------------------------------
# JSON contract
# --------------------------------------------------------------------------
def extract_json(text):
    """Return the review object from model output, or None."""
    candidates = [m.group(1) for m in FENCE_RE.finditer(text)][::-1]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and ("findings" in obj or "verdict" in obj):
            return obj
    return None


def parse_json_review(obj):
    """Validate/normalize. Malformed findings become notes instead of being
    dropped; a malformed entry prevents APPROVE."""
    summary = obj.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    findings, notes = [], []
    raw_findings = obj.get("findings")
    if raw_findings is None:
        raw_findings = []
        notes.append("Reviewer output had no `findings` field.")
    elif not isinstance(raw_findings, list):
        notes.append(f"Reviewer `findings` was not a list: {json.dumps(raw_findings)[:300]}")
        raw_findings = []

    for item in raw_findings:
        if not isinstance(item, dict):
            notes.append(f"Malformed finding: {json.dumps(item)[:300]}")
            continue
        sev = str(item.get("severity", "")).strip().lower()
        desc = str(item.get("description") or item.get("desc") or "").strip()
        file_ = item.get("file")
        line = item.get("line")
        if isinstance(line, str) and line.strip().isdigit():
            line = int(line.strip())
        if isinstance(line, bool) or not (line is None or isinstance(line, int)):
            line = None
        if sev not in SEVERITIES or not desc or not file_:
            notes.append(f"Malformed finding: {json.dumps(item)[:300]}")
            continue
        findings.append(
            {"severity": sev, "file": normalize_path(file_), "line": line, "desc": desc}
        )

    verdict = str(obj.get("verdict", "")).strip().lower().replace(" ", "_")
    if verdict not in VERDICTS:
        verdict = "unknown"
    return summary, findings, notes, verdict


# --------------------------------------------------------------------------
# Legacy markdown fallback
# --------------------------------------------------------------------------
def classify_verdict_text(verdict):
    v = (verdict or "").strip().lower()
    if not v:
        return "unknown"
    if "changes requested" in v or "request changes" in v or "changes_requested" in v:
        return "changes_requested"
    if "with comments" in v or "approve_with_comments" in v:
        return "approve_with_comments"
    if v.startswith(("approve", "lgtm")):
        return "approve"
    return "unknown"


def parse_markdown_review(text):
    """Returns (summary, findings, notes, verdict) or None if the text doesn't
    look like the markdown format at all."""
    def section(name):
        m = re.search(
            rf"^#{{1,4}}\s*{name}\s*:?\s*$(.*?)(?=^#{{1,4}}\s|\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        return m.group(1).strip() if m else None

    summary = section("Summary")
    block = section("Findings")
    verdict = section("Verdict")
    if block is None or verdict is None:
        return None

    findings, notes = [], []
    for raw in block.splitlines():
        line = BULLET_RE.sub("", raw.strip(), count=1).strip()
        if not line or NO_ISSUES_RE.match(line):
            continue
        m = MD_FINDING_RE.match(line)
        if m:
            sev, file_, line_no, desc = m.groups()
            findings.append({
                "severity": sev.lower(),
                "file": normalize_path(file_),
                "line": int(line_no) if line_no else None,
                "desc": desc.strip(),
            })
        else:
            notes.append(line)
    return summary or "", findings, notes, classify_verdict_text(verdict)


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------
VERDICT_LABEL = {
    "approve": "Approve",
    "approve_with_comments": "Approve with comments",
    "changes_requested": "Changes requested",
}


def decide_event(findings, notes, verdict, source, no_approve):
    severities = [f["severity"] for f in findings]
    if "critical" in severities or verdict == "changes_requested":
        return "REQUEST_CHANGES"
    if source != "json" or severities or notes or verdict != "approve":
        return "COMMENT"
    return "COMMENT" if no_approve else "APPROVE"


def format_finding(f):
    loc = f"{f['file']}:{f['line']}" if f["line"] is not None else f["file"]
    return f"**[{f['severity']}]** `{loc}` — {f['desc']}"


def build_review(model_output, diff_text, commit, no_approve=False):
    """Pure function: model output + diff -> (payload dict, stats dict)."""
    obj = extract_json(model_output)
    if obj is not None:
        source = "json"
        summary, findings, notes, verdict = parse_json_review(obj)
    else:
        md = parse_markdown_review(model_output)
        if md is not None:
            source = "markdown"
            summary, findings, notes, verdict = md
        else:
            source = "unparsed"
            summary, findings, notes, verdict = "", [], [], "unknown"

    valid_lines = parse_valid_lines(diff_text)
    inline, body_findings = [], []
    for f in findings:
        if f["line"] is not None and f["line"] in valid_lines.get(f["file"], set()):
            inline.append({
                "path": f["file"],
                "line": f["line"],
                "side": "RIGHT",
                "body": f"**[{f['severity']}]** {f['desc']}",
            })
        elif f["line"] is None:
            body_findings.append(format_finding(f))
        else:
            # GitHub rejects the whole review over one bad line reference.
            body_findings.append(format_finding(f) + " _(line not in diff)_")

    event = decide_event(findings, notes, verdict, source, no_approve)

    parts = []
    if summary:
        parts.append(f"### Summary\n{summary}")
    if inline:
        parts.append(f"_{len(inline)} finding(s) posted as inline comments._")
    if body_findings:
        parts.append("### Findings\n" + "\n".join(f"- {b}" for b in body_findings))
    if notes:
        parts.append("### Reviewer notes\n" + "\n".join(f"- {n}" for n in notes))
    if verdict in VERDICT_LABEL:
        parts.append(f"### Verdict\n{VERDICT_LABEL[verdict]}")
    if source == "unparsed":
        raw = model_output.strip() or "(empty)"
        parts.append(
            "> ⚠️ The reviewer's output didn't match the expected format, so this "
            "review can't approve. Raw output:\n\n```\n" + raw[:60000] + "\n```"
        )
    elif source == "markdown":
        parts.append("_Reviewer returned legacy markdown instead of JSON; approval disabled for this run._")

    payload = {
        "commit_id": commit,
        "body": "\n\n".join(parts) or "AI review produced no output.",
        "event": event,
    }
    if inline:
        payload["comments"] = inline

    severities = [f["severity"] for f in findings]
    stats = {
        "event": event,
        **{s: severities.count(s) for s in SEVERITIES},
        "inline": len(inline),
        "body": len(body_findings),
        "notes": len(notes),
        "verdict": verdict,
        "source": source,
    }
    return payload, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, help="path to the raw (non-annotated) unified diff")
    parser.add_argument("--commit", required=True, help="head commit SHA the review is against")
    parser.add_argument("--no-approve", action="store_true",
                        help="never emit APPROVE; a clean review becomes COMMENT")
    args = parser.parse_args()

    model_output = sys.stdin.read()
    with open(args.diff, "r", encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()

    payload, stats = build_review(model_output, diff_text, args.commit, args.no_approve)
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    print(" ".join(f"{k}={v}" for k, v in stats.items()), file=sys.stderr)


if __name__ == "__main__":
    main()
