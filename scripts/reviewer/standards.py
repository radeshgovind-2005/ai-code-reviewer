"""Engineering standards ("codex") for a repository.

Standards live in the consuming repo under `.review/standards/*.md` and are
read from the BASE commit (a PR can't weaken the rules it is judged by):

    ---
    id: py-no-bare-except
    title: Don't swallow exceptions
    level: MUST            # MUST | SHOULD
    status: enforced       # approved (reported, never blocks) | enforced
    paths: ["*.py"]        # optional, default everything
    ---
    Catching an exception must either handle it, re-raise it, or log it with
    context. `except Exception: pass` is never acceptable.

Only standards whose `paths` match a changed file are sent to the model, in a
compact form. After the review, severities are adjusted deterministically:
  MUST + enforced  -> critical (blocks)
  MUST + approved  -> at most warning
  SHOULD           -> at most warning
"""
from __future__ import annotations

import json
import re
import subprocess

from .paths import any_match

STANDARDS_DIR = ".review/standards"
SEV_ORDER = ["suggestion", "warning", "critical"]


def _git(repo_dir, *args):
    return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, check=False)


def parse_front_matter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return None, text
    meta = {}
    for line in m.group(1).splitlines():
        line = line.split(" #", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if value.startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                value = [v.strip().strip("'\"") for v in value.strip("[]").split(",") if v.strip()]
        else:
            value = value.strip("'\"")
        meta[key.strip()] = value
    return meta, m.group(2).strip()


def load_from_git(repo_dir, sha):
    """-> list of standard dicts found at `sha` (empty if none)."""
    ls = _git(repo_dir, "ls-tree", "-r", "--name-only", sha, "--", STANDARDS_DIR)
    if ls.returncode != 0:
        return []
    out = []
    for path in sorted(p for p in ls.stdout.splitlines() if p.endswith(".md")):
        show = _git(repo_dir, "show", f"{sha}:{path}")
        if show.returncode != 0:
            continue
        meta, body = parse_front_matter(show.stdout)
        std = normalize(meta, body, path)
        if std:
            out.append(std)
    return out


def normalize(meta, body, path):
    if not meta or not meta.get("id"):
        return None
    level = str(meta.get("level", "SHOULD")).upper()
    status = str(meta.get("status", "approved")).lower()
    paths = meta.get("paths") or ["**"]
    if isinstance(paths, str):
        paths = [paths]
    return {
        "id": str(meta["id"]),
        "title": str(meta.get("title", "")),
        "level": level if level in ("MUST", "SHOULD") else "SHOULD",
        "status": status if status in ("approved", "enforced") else "approved",
        "paths": [str(p) for p in paths],
        "body": body[:2000],
        "source": path,
    }


def applicable(standards, changed_files):
    return [s for s in standards if any_match(changed_files, s["paths"])]


def to_prompt(standards):
    lines = []
    for s in standards:
        lines.append(f"### `{s['id']}` — {s['level']} ({s['status']}) — {s['title']}")
        lines.append(f"Applies to: {', '.join(s['paths'])}")
        lines.append(s["body"])
        lines.append("")
    return "\n".join(lines).strip()


def enforce(findings, standards):
    """Adjust severities of findings that cite a standard. Mutates and returns
    findings; unknown rule ids are left alone (and the rule_id is dropped)."""
    by_id = {s["id"]: s for s in standards}
    for f in findings:
        rid = f.get("rule_id")
        if not rid:
            continue
        std = by_id.get(str(rid))
        if not std:
            f.pop("rule_id", None)
            continue
        if std["level"] == "MUST" and std["status"] == "enforced":
            f["severity"] = "critical"
        elif f.get("severity") not in SEV_ORDER or SEV_ORDER.index(f["severity"]) > SEV_ORDER.index("warning"):
            f["severity"] = "warning"
        f["description"] = f"[{std['level']} `{std['id']}`] {f.get('description', '')}"
    return findings
