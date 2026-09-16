#!/usr/bin/env python3
"""Normalizes raw scanner outputs into one findings file.

  normalize-scanners.py --raw-dir RAW --config CONFIG --diff DIFF \
      --changed-files CHANGED --out-json findings.json --out-md summary.md \
      [--out-sarif findings.sarif]

RAW contains whatever each scanner produced (missing file = tool skipped):
  gitleaks.json  osv.json  opengrep.json  trivy.json  zizmor.json  actionlint.json
and optionally <tool>.status containing "failed: <reason>" or "skipped: <reason>".

Every finding becomes:
  {tool, rule, severity: critical|high|medium|low|info, file, line|null,
   message, blocking: bool, in_diff: bool}

Per-tool config (review-config.json "scanners.<tool>"):
  enabled (default true), ignore_rules (list of rule ids to drop), block_on.

Blocking policy ("block_on"):
  "none" | "critical" | "high" | "medium" | "low" -- findings at or above that
  severity block, but only when they are introduced by this PR:
    - gitleaks: always new (it scans the PR's commit range)
    - osv: the manifest/lockfile was changed in this PR
    - everything else: the finding's line was added/changed in the diff (or
      the finding has no line and its file changed)

Exit code: 0 always (the caller decides what to do with blocking_count).
"""
import argparse
import json
import os
import re
import sys

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]
DEFAULT_BLOCK_ON = {
    "gitleaks": "low",       # any secret blocks
    "osv": "high",
    "opengrep": "none",
    "trivy": "critical",
    "zizmor": "high",
    "actionlint": "none",
}
TOOLS = list(DEFAULT_BLOCK_ON)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def sev_rank(s):
    return SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else SEVERITY_ORDER.index("medium")


# --------------------------------------------------------------------------
# Diff helpers
# --------------------------------------------------------------------------
def added_lines(diff_text):
    """file -> set of new-side line numbers that were added in the diff."""
    out, cur, new = {}, None, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            new = None
            continue
        m = HUNK_RE.match(line)
        if m:
            new = int(m.group(1))
            continue
        if cur is None or new is None or line.startswith("--- "):
            continue
        if line.startswith("+"):
            out.setdefault(cur, set()).add(new)
            new += 1
        elif line.startswith(" "):
            new += 1
    return out


def norm_path(p):
    p = str(p or "").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


# --------------------------------------------------------------------------
# Parsers: raw tool output -> list of partial findings
# --------------------------------------------------------------------------
def parse_gitleaks(data):
    for x in data or []:
        yield {
            "rule": x.get("RuleID", "secret"),
            "severity": "critical",
            "file": norm_path(x.get("File")),
            "line": x.get("StartLine"),
            "message": f"{x.get('Description', 'Secret detected')} (commit {str(x.get('Commit', ''))[:7]}). "
                       "Rotate the secret; removing it from the branch is not enough.",
        }


def cvss_to_severity(score):
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "medium"
    if s >= 9.0:
        return "critical"
    if s >= 7.0:
        return "high"
    if s >= 4.0:
        return "medium"
    return "low" if s > 0 else "info"


def parse_osv(data):
    for result in (data or {}).get("results", []):
        path = norm_path((result.get("source") or {}).get("path"))
        for pkg in result.get("packages", []):
            p = pkg.get("package", {})
            vulns = {v.get("id"): v for v in pkg.get("vulnerabilities", [])}
            for group in pkg.get("groups") or [{"ids": list(vulns), "max_severity": ""}]:
                ids = group.get("ids") or []
                fixed = sorted({
                    ev["fixed"]
                    for vid in ids for aff in (vulns.get(vid) or {}).get("affected", [])
                    for rng in aff.get("ranges", []) for ev in rng.get("events", []) if "fixed" in ev
                })
                summary = next((vulns[v].get("summary") for v in ids if v in vulns and vulns[v].get("summary")), "")
                aliases = [a for a in group.get("aliases", []) if a.startswith("CVE-")]
                msg = f"{p.get('name')} {p.get('version')}: {', '.join(ids + aliases)}"
                if summary:
                    msg += f" — {summary}"
                msg += f". Fixed in: {', '.join(fixed)}." if fixed else ". No fixed version listed."
                yield {
                    "rule": ids[0] if ids else "vulnerable-dependency",
                    "severity": cvss_to_severity(group.get("max_severity")),
                    "file": path,
                    "line": None,
                    "message": msg,
                }


OPENGREP_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}


def parse_opengrep(data):
    for r in (data or {}).get("results", []):
        extra = r.get("extra", {})
        yield {
            "rule": ".".join(r.get("check_id", "rule").split(".")[-2:]),
            "severity": OPENGREP_SEV.get(str(extra.get("severity", "")).upper(), "medium"),
            "file": norm_path(r.get("path")),
            "line": (r.get("start") or {}).get("line"),
            "message": extra.get("message", ""),
        }


def parse_trivy(data):
    for res in (data or {}).get("Results", []) or []:
        target = norm_path(res.get("Target"))
        for m in res.get("Misconfigurations") or []:
            cause = m.get("CauseMetadata") or {}
            yield {
                "rule": m.get("ID") or m.get("AVDID") or "misconfig",
                "severity": str(m.get("Severity", "MEDIUM")).lower(),
                "file": target,
                "line": cause.get("StartLine"),
                "message": f"{m.get('Title', '')}: {m.get('Message', '')}".strip(": "),
            }


ZIZMOR_SEV = {"informational": "info", "unknown": "medium", "low": "low", "medium": "medium", "high": "high"}


def parse_zizmor(data):
    for f in data or []:
        det = f.get("determinations", {})
        loc = next((l for l in f.get("locations", []) if (l.get("symbolic") or {}).get("kind") == "Primary"),
                   (f.get("locations") or [{}])[0])
        key = ((loc.get("symbolic") or {}).get("key") or {}).get("Local") or {}
        row = (((loc.get("concrete") or {}).get("location") or {}).get("start_point") or {}).get("row")
        annotation = (loc.get("symbolic") or {}).get("annotation", "")
        yield {
            "rule": f.get("ident", "zizmor"),
            "severity": ZIZMOR_SEV.get(str(det.get("severity", "")).lower(), "medium"),
            "file": norm_path(key.get("given_path") or key.get("verbatim_path")),
            "line": row + 1 if isinstance(row, int) else None,
            "message": f"{f.get('desc', '')}: {annotation} (confidence {det.get('confidence', '?')}; {f.get('url', '')})",
        }


def parse_actionlint(data):
    for e in data or []:
        msg = e.get("message", "")
        yield {
            "rule": e.get("kind", "actionlint"),
            "severity": "high" if "untrusted" in msg else "medium",
            "file": norm_path(e.get("filepath")),
            "line": e.get("line"),
            "message": msg,
        }


PARSERS = {
    "gitleaks": parse_gitleaks,
    "osv": parse_osv,
    "opengrep": parse_opengrep,
    "trivy": parse_trivy,
    "zizmor": parse_zizmor,
    "actionlint": parse_actionlint,
}


# --------------------------------------------------------------------------
# Main logic (pure)
# --------------------------------------------------------------------------
def normalize(raw, statuses, config, diff_text, changed_files):
    """raw: tool -> parsed JSON (or None if absent). Returns the findings doc."""
    scanners_cfg = (config or {}).get("scanners") or {}
    added = added_lines(diff_text)
    changed = set(changed_files)
    findings, tools = [], {}

    for tool in TOOLS:
        cfg = scanners_cfg.get(tool) or {}
        status = statuses.get(tool)
        if cfg.get("enabled") is False:
            tools[tool] = {"status": "disabled", "count": 0}
            continue
        if raw.get(tool) is None:
            tools[tool] = {"status": status or "skipped: nothing to scan", "count": 0}
            continue
        block_on = cfg.get("block_on", DEFAULT_BLOCK_ON[tool])
        ignore_rules = set(cfg.get("ignore_rules") or [])
        try:
            parsed = list(PARSERS[tool](raw[tool]))
        except Exception as exc:  # malformed tool output must not crash the run
            tools[tool] = {"status": f"failed: could not parse output ({exc.__class__.__name__})", "count": 0}
            continue
        count = 0
        for f in parsed:
            f["tool"] = tool
            if f["rule"] in ignore_rules:
                continue
            line = f.get("line")
            f["line"] = line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None
            if tool == "gitleaks":
                in_diff = True
            elif tool == "osv":
                in_diff = f["file"] in changed
            else:
                if f["file"] not in changed:
                    continue  # only report on files this PR touches
                in_diff = f["line"] is None or f["line"] in added.get(f["file"], set())
            f["in_diff"] = in_diff
            f["blocking"] = bool(
                block_on != "none" and in_diff and sev_rank(f["severity"]) >= sev_rank(block_on)
            )
            findings.append(f)
            count += 1
        tools[tool] = {"status": status or "ok", "count": count}

    findings.sort(key=lambda f: (not f["blocking"], -sev_rank(f["severity"]), f["file"], f["line"] or 0))
    return {
        "findings": findings,
        "tools": tools,
        "blocking_count": sum(f["blocking"] for f in findings),
    }


SEV_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵", "info": "⚪"}


def to_markdown(doc, max_items=50):
    lines = []
    blocking = doc["blocking_count"]
    total = len(doc["findings"])
    head = f"**{total}** scanner finding(s)"
    head += f", **{blocking} blocking**" if blocking else ", none blocking"
    lines.append(head)
    lines.append("")
    for f in doc["findings"][:max_items]:
        loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        tag = " **[blocking]**" if f["blocking"] else ("" if f["in_diff"] else " _(pre-existing)_")
        msg = " ".join(str(f["message"]).split())[:400]
        lines.append(f"- {SEV_EMOJI.get(f['severity'], '')} `{f['tool']}` `{f['rule']}` `{loc}`{tag} — {msg}")
    if total > max_items:
        lines.append(f"- … {total - max_items} more (see the `scanner-findings` artifact)")
    lines.append("")
    lines.append("| tool | status | findings |")
    lines.append("|---|---|---|")
    for tool, t in doc["tools"].items():
        lines.append(f"| {tool} | {t['status']} | {t['count']} |")
    return "\n".join(lines) + "\n"


SARIF_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}


def to_sarif(doc):
    rules, results = {}, []
    for f in doc["findings"]:
        rid = f"{f['tool']}/{f['rule']}"
        rules.setdefault(rid, {"id": rid, "shortDescription": {"text": rid}})
        loc = {"physicalLocation": {"artifactLocation": {"uri": f["file"]}}}
        if f["line"]:
            loc["physicalLocation"]["region"] = {"startLine": f["line"]}
        results.append({
            "ruleId": rid,
            "level": SARIF_LEVEL.get(f["severity"], "warning"),
            "message": {"text": str(f["message"])},
            "locations": [loc],
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "ai-code-reviewer-scanners", "rules": list(rules.values())}},
            "results": results,
        }],
    }


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read().strip()
    if not text:
        return [] if path.endswith(("gitleaks.json", "zizmor.json", "actionlint.json")) else {}
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--changed-files", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-sarif")
    args = ap.parse_args()

    raw, statuses = {}, {}
    for tool in TOOLS:
        status_file = os.path.join(args.raw_dir, f"{tool}.status")
        if os.path.exists(status_file):
            with open(status_file) as fh:
                statuses[tool] = fh.read().strip()
        try:
            raw[tool] = load_json(os.path.join(args.raw_dir, f"{tool}.json"))
        except ValueError:
            raw[tool] = None
            statuses[tool] = "failed: output was not valid JSON"
        if statuses.get(tool, "").startswith(("failed", "skipped")):
            raw[tool] = None

    with open(args.config) as fh:
        config = json.load(fh)
    with open(args.diff, encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()
    with open(args.changed_files) as fh:
        changed = [norm_path(l) for l in fh if l.strip()]

    doc = normalize(raw, statuses, config, diff_text, changed)
    with open(args.out_json, "w") as fh:
        json.dump(doc, fh, indent=2)
    with open(args.out_md, "w") as fh:
        fh.write(to_markdown(doc))
    if args.out_sarif:
        with open(args.out_sarif, "w") as fh:
            json.dump(to_sarif(doc), fh, indent=2)
    print(f"scanner_findings={len(doc['findings'])} blocking={doc['blocking_count']}", file=sys.stderr)


if __name__ == "__main__":
    main()
