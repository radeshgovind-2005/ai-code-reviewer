#!/usr/bin/env bash
set -Eeuo pipefail

# Runs the free deterministic scanners on a PR and writes, into OUT_DIR:
#   raw/<tool>.json          raw tool output (+ raw/<tool>.status on skip/failure)
#   scanner-findings.json    normalized findings (see normalize-scanners.py)
#   scanner-summary.md       markdown summary (also appended to the step summary)
#   scanner-findings.sarif   SARIF 2.1.0 for optional code-scanning upload
#
# Expects: BASE_SHA, HEAD_SHA, REVIEWER_HOME; run from the target repo root.
# Optional: OUT_DIR (default ./.ai-review-scan), OPENGREP_RULES_DIR,
#           SCANNER_TIMEOUT (seconds per tool, default 300)
#
# Exit codes: 0 = no blocking findings, 1 = blocking findings, 2 = setup error.
# A scanner that crashes or times out is reported as "failed" and does NOT
# block (unless scanners.fail_on_error is true in the config).

REVIEWER_HOME="${REVIEWER_HOME:?REVIEWER_HOME not set}"
: "${BASE_SHA:?BASE_SHA not set}" "${HEAD_SHA:?HEAD_SHA not set}"
OUT_DIR="$(mkdir -p "${OUT_DIR:-.ai-review-scan}" && cd "${OUT_DIR:-.ai-review-scan}" && pwd)"
RAW="${OUT_DIR}/raw"
mkdir -p "${RAW}"
SCANNER_TIMEOUT="${SCANNER_TIMEOUT:-300}"
OPENGREP_RULES_DIR="${OPENGREP_RULES_DIR:-$HOME/.cache/opengrep-rules}"
export LANG=C.UTF-8 LC_ALL=C.UTF-8  # opengrep crashes on non-ASCII output otherwise

# shellcheck source=scripts/lib.sh
source "${REVIEWER_HOME}/scripts/lib.sh"

CONFIG_FILE="${OUT_DIR}/review-config.json"
resolve_config "${CONFIG_FILE}" || { echo "error: review-config.json on the base branch is not valid JSON" >&2; exit 2; }

enabled() { [ "$(jq -r --arg t "$1" '.scanners[$t].enabled // true' "${CONFIG_FILE}")" != "false" ]; }
mark() { echo "$2" > "${RAW}/$1.status"; }

# run_tool NAME OUTFILE CMD... : runs with timeout; a non-zero exit only counts
# as failure when the tool didn't produce valid JSON (several scanners exit 1
# when they find something).
LAST_RC=0
run_tool() {
  local name="$1" out="$2" rc=0
  shift 2
  echo "::group::${name}"
  timeout "${SCANNER_TIMEOUT}" "$@" || rc=$?
  echo "::endgroup::"
  LAST_RC="${rc}"
  if [ "${rc}" -eq 124 ]; then
    mark "${name}" "failed: timed out after ${SCANNER_TIMEOUT}s"
  elif [ ! -s "${out}" ] && [ "${rc}" -ne 0 ]; then
    mark "${name}" "failed: exited ${rc} without output"
  elif [ -s "${out}" ] && ! jq empty "${out}" 2>/dev/null; then
    mark "${name}" "failed: exited ${rc}, output not JSON"
  fi
}

have() {
  if command -v "$1" > /dev/null; then return 0; fi
  mark "$2" "failed: $1 not installed"
  return 1
}

# Changed files (added/copied/modified/renamed; deletions can't have findings).
CHANGED="${OUT_DIR}/changed-files.txt"
git diff --name-only --diff-filter=ACMR "${BASE_SHA}...${HEAD_SHA}" > "${CHANGED}"
DIFF="${OUT_DIR}/pr.diff"
git diff "${BASE_SHA}...${HEAD_SHA}" > "${DIFF}"

changed_matching() { grep -E "$1" "${CHANGED}" || true; }

# --- gitleaks: secrets in the PR's commits --------------------------------
#     gitleaks would pick up .gitleaks.toml / .gitleaksignore from the PR
#     checkout, letting a PR allowlist its own secret. Always pass the BASE
#     branch's versions (or the built-in defaults) explicitly.
if enabled gitleaks && have gitleaks gitleaks; then
  GL_CONFIG="${OUT_DIR}/gitleaks.toml"
  GL_IGNORE="${OUT_DIR}/gitleaksignore"
  if git cat-file -e "${BASE_SHA}:.gitleaks.toml" 2>/dev/null; then
    git show "${BASE_SHA}:.gitleaks.toml" > "${GL_CONFIG}"
  else
    printf '[extend]\nuseDefault = true\n' > "${GL_CONFIG}"
  fi
  if git cat-file -e "${BASE_SHA}:.gitleaksignore" 2>/dev/null; then
    git show "${BASE_SHA}:.gitleaksignore" > "${GL_IGNORE}"
  else
    : > "${GL_IGNORE}"
  fi
  run_tool gitleaks "${RAW}/gitleaks.json" \
    gitleaks git --log-opts="${BASE_SHA}..${HEAD_SHA}" --config "${GL_CONFIG}" \
      --gitleaks-ignore-path "${GL_IGNORE}" --report-format json \
      --report-path "${RAW}/gitleaks.json" --no-banner --exit-code 0 --redact .
fi

# --- osv-scanner: vulnerable dependencies (whole repo; blocks only if a
#     manifest/lockfile changed) -------------------------------------------
if enabled osv && have osv-scanner osv; then
  run_tool osv "${RAW}/osv.json" \
    osv-scanner scan source --format json --output-file "${RAW}/osv.json" -r .
  # osv-scanner: 0 clean, 1 vulns, 127 error (e.g. API unreachable), 128 no packages.
  case "${LAST_RC}" in
    127) mark osv "failed: osv-scanner error (exit 127; is api.osv.dev reachable?)" ;;
    128) mark osv "skipped: no dependency manifests found" ;;
  esac
fi

# --- opengrep: SAST on changed source files, rules picked by language -----
if enabled opengrep && have opengrep opengrep; then
  mapfile -t CODE_FILES < <(changed_matching '\.(py|js|jsx|mjs|cjs|ts|tsx|go|java|kt|rb|php|cs|rs|scala|swift|c|cc|cpp|h|sh|bash)$')
  EXISTING=()
  for f in "${CODE_FILES[@]}"; do [ -f "$f" ] && EXISTING+=("$f"); done
  if [ "${#EXISTING[@]}" -eq 0 ]; then
    mark opengrep "skipped: no changed source files"
  elif [ ! -d "${OPENGREP_RULES_DIR}" ]; then
    mark opengrep "failed: rules not found at ${OPENGREP_RULES_DIR}"
  else
    declare -A LANG_DIRS=()
    for f in "${EXISTING[@]}"; do
      case "$f" in
        *.py) LANG_DIRS[python]=1 ;;
        *.js|*.jsx|*.mjs|*.cjs) LANG_DIRS[javascript]=1 ;;
        *.ts|*.tsx) LANG_DIRS[typescript]=1; LANG_DIRS[javascript]=1 ;;
        *.go) LANG_DIRS[go]=1 ;;
        *.java) LANG_DIRS[java]=1 ;;
        *.kt) LANG_DIRS[kotlin]=1 ;;
        *.rb) LANG_DIRS[ruby]=1 ;;
        *.php) LANG_DIRS[php]=1 ;;
        *.cs) LANG_DIRS[csharp]=1 ;;
        *.rs) LANG_DIRS[rust]=1 ;;
        *.scala) LANG_DIRS[scala]=1 ;;
        *.swift) LANG_DIRS[swift]=1 ;;
        *.c|*.cc|*.cpp|*.h) LANG_DIRS[c]=1 ;;
        *.sh|*.bash) LANG_DIRS[bash]=1 ;;
      esac
    done
    CONFIG_ARGS=()
    for lang in "${!LANG_DIRS[@]}"; do
      [ -d "${OPENGREP_RULES_DIR}/${lang}" ] && CONFIG_ARGS+=(--config "${OPENGREP_RULES_DIR}/${lang}")
    done
    if [ "${#CONFIG_ARGS[@]}" -eq 0 ]; then
      mark opengrep "skipped: no rules for the changed languages"
    else
      run_tool opengrep "${RAW}/opengrep.json" \
        opengrep scan "${CONFIG_ARGS[@]}" --json --output "${RAW}/opengrep.json" --quiet "${EXISTING[@]}"
    fi
  fi
fi

# --- trivy config: Dockerfile / Terraform / Kubernetes / Helm -------------
if enabled trivy; then
  if [ -z "$(changed_matching '(^|/)(Dockerfile[^/]*|[^/]*\.dockerfile|[^/]*\.tf|[^/]*\.tfvars|Chart\.yaml|values[^/]*\.ya?ml|[^/]*k8s[^/]*\.ya?ml|docker-compose[^/]*\.ya?ml)$')" ]; then
    mark trivy "skipped: no infrastructure files changed"
  elif have trivy trivy; then
    run_tool trivy "${RAW}/trivy.json" \
      trivy config --format json --output "${RAW}/trivy.json" --quiet .
  fi
fi

# --- zizmor + actionlint: GitHub Actions workflows ------------------------
mapfile -t WORKFLOWS < <(changed_matching '^\.github/workflows/[^/]+\.ya?ml$')
if [ "${#WORKFLOWS[@]}" -eq 0 ]; then
  enabled zizmor && mark zizmor "skipped: no workflow files changed"
  enabled actionlint && mark actionlint "skipped: no workflow files changed"
else
  if enabled zizmor && have zizmor zizmor; then
    # zizmor exits non-zero when it finds something; --offline avoids GitHub API calls.
    run_tool zizmor "${RAW}/zizmor.json" \
      sh -c 'zizmor --format json --offline "$@" > "$0"' "${RAW}/zizmor.json" "${WORKFLOWS[@]}"
  fi
  if enabled actionlint && have actionlint actionlint; then
    run_tool actionlint "${RAW}/actionlint.json" \
      sh -c 'actionlint -format "{{json .}}" "$@" > "$0"' "${RAW}/actionlint.json" "${WORKFLOWS[@]}"
  fi
fi

# --- normalize -------------------------------------------------------------
python3 "${REVIEWER_HOME}/scripts/normalize-scanners.py" \
  --raw-dir "${RAW}" --config "${CONFIG_FILE}" --diff "${DIFF}" --changed-files "${CHANGED}" \
  --out-json "${OUT_DIR}/scanner-findings.json" --out-md "${OUT_DIR}/scanner-summary.md" \
  --out-sarif "${OUT_DIR}/scanner-findings.sarif"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  { echo "### Scanners"; echo ""; cat "${OUT_DIR}/scanner-summary.md"; } >> "${GITHUB_STEP_SUMMARY}"
fi
cat "${OUT_DIR}/scanner-summary.md"

BLOCKING="$(jq '.blocking_count' "${OUT_DIR}/scanner-findings.json")"
FAILED="$(jq '[.tools[] | select(.status | startswith("failed"))] | length' "${OUT_DIR}/scanner-findings.json")"
if [ "${BLOCKING}" -gt 0 ]; then
  echo "error: ${BLOCKING} blocking scanner finding(s)" >&2
  exit 1
fi
if [ "${FAILED}" -gt 0 ] && [ "$(jq -r '.scanners.fail_on_error // false' "${CONFIG_FILE}")" = "true" ]; then
  echo "error: ${FAILED} scanner(s) failed and scanners.fail_on_error is true" >&2
  exit 1
fi
exit 0
