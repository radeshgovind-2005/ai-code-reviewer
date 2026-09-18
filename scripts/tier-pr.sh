#!/usr/bin/env bash
set -euo pipefail

# Classifies a PR into trivial / lite / full based on diff stats alone --
# no model call. Prints three lines to stdout:
#   TIER=<trivial|lite|full>
#   SENSITIVE=<true|false>
#   REASON=<short human-readable reason>
#
# Expects (already set by the caller):
#   BASE_SHA, HEAD_SHA   - commit range to diff
#   REVIEWER_HOME        - path to this reviewer repo checkout (for default config)
#   CONFIG_FILE          - (optional) resolved config; run-review.sh passes the
#                          one read from the BASE commit, so a PR can't loosen
#                          its own review rules
# Run from inside the target repo's working directory.
#
# Size stats ignore noise (lockfiles, dist, vendor, ...: scripts/diff-excludes.txt)
# so a lockfile bump doesn't push a PR to full. The sensitive-path check looks
# at EVERY changed file, noise included -- it errs on the side of caution.
#
# Sensitive path patterns (review-config.json "sensitive_paths"):
#   "auth/"          directory named auth at any depth (auth/x, src/auth/x)
#   "src/auth/"      that directory path at any depth
#   "Dockerfile"     file with that name at any depth
#   "*.pem"          anything containing * ? or [ is a shell glob on the full
#                    path; * also matches /, so "*.pem" matches at any depth

REVIEWER_HOME="${REVIEWER_HOME:?REVIEWER_HOME not set}"

if [ -z "${CONFIG_FILE:-}" ]; then
  if [ -f "review-config.json" ]; then
    CONFIG_FILE="review-config.json"
  else
    CONFIG_FILE="${REVIEWER_HOME}/review-config.json"
  fi
fi

# shellcheck source=scripts/lib.sh
source "${REVIEWER_HOME}/scripts/lib.sh"
load_excludes

SENSITIVE_PATHS=()
while IFS= read -r p; do
  [ -n "$p" ] && SENSITIVE_PATHS+=("$p")
done < <(jq -r '(.sensitive_paths // [])[]' "${CONFIG_FILE}")
TRIVIAL_MAX_LINES=$(jq -r '.thresholds.trivial.max_lines' "${CONFIG_FILE}")
TRIVIAL_MAX_FILES=$(jq -r '.thresholds.trivial.max_files' "${CONFIG_FILE}")
LITE_MAX_LINES=$(jq -r '.thresholds.lite.max_lines' "${CONFIG_FILE}")
LITE_MAX_FILES=$(jq -r '.thresholds.lite.max_files' "${CONFIG_FILE}")

# Returns 0 if path $1 matches sensitive pattern $2.
matches_sensitive() {
  local f="$1" p="$2"
  if [[ "$p" == *[\*\?\[]* ]]; then
    # shellcheck disable=SC2053  # intentional glob match
    [[ "$f" == $p ]]
  elif [[ "$p" == */ ]]; then
    [[ "$f" == "$p"* || "$f" == */"$p"* ]]
  else
    [[ "$f" == "$p" || "$f" == */"$p" || "$f" == "$p"/* || "$f" == */"$p"/* ]]
  fi
}

ALL_CHANGED="$(git diff --name-only "${BASE_SHA}...${HEAD_SHA}")"
SENSITIVE_HIT=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  for pattern in "${SENSITIVE_PATHS[@]}"; do
    if matches_sensitive "$f" "$pattern"; then
      SENSITIVE_HIT="$f"
      break 2
    fi
  done
done <<< "${ALL_CHANGED}"

if [ -n "${SENSITIVE_HIT}" ]; then
  echo "TIER=full"
  echo "SENSITIVE=true"
  echo "REASON=touches sensitive path (${SENSITIVE_HIT})"
  exit 0
fi

FILES_CHANGED="$(git diff --name-only "${BASE_SHA}...${HEAD_SHA}" -- . "${EXCLUDES[@]}" | grep -c . || true)"
SHORTSTAT="$(git diff --shortstat "${BASE_SHA}...${HEAD_SHA}" -- . "${EXCLUDES[@]}")"
LINES_ADDED="$(echo "${SHORTSTAT}" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)"
LINES_REMOVED="$(echo "${SHORTSTAT}" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)"
LINES_CHANGED=$((LINES_ADDED + LINES_REMOVED))

if [ "${FILES_CHANGED}" -le "${TRIVIAL_MAX_FILES}" ] && [ "${LINES_CHANGED}" -le "${TRIVIAL_MAX_LINES}" ]; then
  echo "TIER=trivial"
  echo "SENSITIVE=false"
  echo "REASON=${LINES_CHANGED} lines / ${FILES_CHANGED} files (<= trivial threshold)"
  exit 0
fi

if [ "${FILES_CHANGED}" -le "${LITE_MAX_FILES}" ] && [ "${LINES_CHANGED}" -le "${LITE_MAX_LINES}" ]; then
  echo "TIER=lite"
  echo "SENSITIVE=false"
  echo "REASON=${LINES_CHANGED} lines / ${FILES_CHANGED} files (<= lite threshold)"
  exit 0
fi

echo "TIER=full"
echo "SENSITIVE=false"
echo "REASON=${LINES_CHANGED} lines / ${FILES_CHANGED} files (exceeds lite threshold)"
