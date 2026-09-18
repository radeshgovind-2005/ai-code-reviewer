#!/usr/bin/env bash
set -euo pipefail

# Runs the same scanners + AI review as CI on your local changes, and prints
# the review instead of posting it.
#
#   scripts/review-local.sh [--base REF] [--scan] [--no-ai] [--json]
#
#   --base REF   compare against REF (default: origin/HEAD, else main)
#   --scan       also run the scanners (needs them installed:
#                scripts/install-scanners.sh ~/.local/bin -- linux/amd64 only,
#                or install gitleaks/opengrep/... yourself)
#   --no-ai      scanners only
#   --json       print the raw review payload
#
# Reviews committed AND uncommitted changes to tracked files (untracked files
# are ignored -- `git add` them first). Needs: git, jq, python3, opencode and a
# provider key (LLM_API_KEY or e.g. ANTHROPIC_API_KEY). Run it from inside the
# repository you want reviewed.

REVIEWER_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_REF=""
SCAN=false
AI=true
JSON=false
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE_REF="$2"; shift 2 ;;
    --scan) SCAN=true; shift ;;
    --no-ai) AI=false; shift ;;
    --json) JSON=true; shift ;;
    -h|--help) sed -n '4,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"
if [ -z "${BASE_REF}" ]; then
  BASE_REF="$(git symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null || echo main)"
fi
BASE_SHA="$(git merge-base "${BASE_REF}" HEAD)"
# A commit object of the working tree (tracked files), without touching it.
HEAD_SHA="$(git stash create 2>/dev/null || true)"
[ -n "${HEAD_SHA}" ] || HEAD_SHA="$(git rev-parse HEAD)"
export BASE_SHA HEAD_SHA REVIEWER_HOME

OUT="$(mktemp -d)"
trap 'rm -rf "${OUT}"' EXIT
echo "Reviewing ${BASE_REF} (${BASE_SHA:0:7}) → working tree" >&2

SCAN_RC=0
if [ "${SCAN}" = true ]; then
  OUT_DIR="${OUT}/scan" bash "${REVIEWER_HOME}/scripts/run-scanners.sh" | grep -v '^::' || SCAN_RC=$?
  export SCANNER_FINDINGS="${OUT}/scan/scanner-findings.json"
fi
[ "${AI}" = true ] || exit "${SCAN_RC}"

export DRY_RUN=1 DRY_RUN_OUT="${OUT}/payload.json"
export GITHUB_REPOSITORY="local/review" PR_NUMBER=0 GITHUB_RUN_ID=local
export REVIEW_ARTIFACTS_DIR="${OUT}/artifacts"
PR_TITLE="$(git log -1 --format=%s)"
export PR_TITLE
bash "${REVIEWER_HOME}/scripts/run-review.sh" >&2

if [ ! -s "${OUT}/payload.json" ]; then
  echo "No review produced (nothing to review?)." >&2
  exit 0
fi
if [ "${JSON}" = true ]; then
  cat "${OUT}/payload.json"
  exit 0
fi
python3 - "${OUT}/payload.json" <<'PY'
import json, re, sys
p = json.load(open(sys.argv[1]))
colors = {"REQUEST_CHANGES": "\033[31m", "COMMENT": "\033[33m", "APPROVE": "\033[32m"}
tty = sys.stdout.isatty()
c = colors.get(p["event"], "") if tty else ""
reset = "\033[0m" if tty else ""
print(f"\n{c}== {p['event']} =={reset}\n")
print(re.sub(r"</?sub>", "", p["body"]))
for cm in p.get("comments", []):
    body = "\n".join(l.lstrip("> ") for l in cm["body"].splitlines() if not l.startswith("> [!"))
    print(f"\n--- {cm['path']}:{cm['line']}\n{body}")
PY
