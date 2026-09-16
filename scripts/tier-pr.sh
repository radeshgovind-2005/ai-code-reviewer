#!/usr/bin/env bash
set -euo pipefail

# Classifies a PR into trivial / lite / full based on diff stats alone --
# no model call. Prints two lines to stdout:
#   TIER=<trivial|lite|full>
#   REASON=<short human-readable reason>
#
# Expects (already set by the caller):
#   BASE_SHA, HEAD_SHA   - commit range to diff
#   REVIEWER_HOME        - path to this reviewer repo checkout (for default config)
# Run from inside the target repo's working directory.

REVIEWER_HOME="${REVIEWER_HOME:?REVIEWER_HOME not set}"

# Config resolution: consuming repo's own review-config.json wins; else
# fall back to this reviewer repo's default.
if [ -f "review-config.json" ]; then
  CONFIG_FILE="review-config.json"
else
  CONFIG_FILE="${REVIEWER_HOME}/review-config.json"
fi

SENSITIVE_PATHS=($(jq -r '.sensitive_paths[]' "${CONFIG_FILE}"))
TRIVIAL_MAX_LINES=$(jq -r '.thresholds.trivial.max_lines' "${CONFIG_FILE}")
TRIVIAL_MAX_FILES=$(jq -r '.thresholds.trivial.max_files' "${CONFIG_FILE}")
LITE_MAX_LINES=$(jq -r '.thresholds.lite.max_lines' "${CONFIG_FILE}")
LITE_MAX_FILES=$(jq -r '.thresholds.lite.max_files' "${CONFIG_FILE}")

CHANGED_FILES_RAW="$(git diff --name-only "${BASE_SHA}...${HEAD_SHA}")"
FILES_CHANGED="$(echo "${CHANGED_FILES_RAW}" | grep -c . || true)"

# Lines changed = added + removed, from --shortstat.
SHORTSTAT="$(git diff --shortstat "${BASE_SHA}...${HEAD_SHA}")"
LINES_ADDED="$(echo "${SHORTSTAT}" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)"
LINES_REMOVED="$(echo "${SHORTSTAT}" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)"
LINES_CHANGED=$((LINES_ADDED + LINES_REMOVED))

# Sensitive-path check: any changed file matching any configured prefix
# forces full tier, regardless of size.
SENSITIVE_HIT=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  for prefix in "${SENSITIVE_PATHS[@]}"; do
    if [[ "$f" == "$prefix"* ]]; then
      SENSITIVE_HIT="$f"
      break 2
    fi
  done
done <<< "${CHANGED_FILES_RAW}"

if [ -n "${SENSITIVE_HIT}" ]; then
  echo "TIER=full"
  echo "REASON=touches sensitive path (${SENSITIVE_HIT})"
  exit 0
fi

if [ "${FILES_CHANGED}" -le "${TRIVIAL_MAX_FILES}" ] && [ "${LINES_CHANGED}" -le "${TRIVIAL_MAX_LINES}" ]; then
  echo "TIER=trivial"
  echo "REASON=${LINES_CHANGED} lines / ${FILES_CHANGED} files (<= trivial threshold)"
  exit 0
fi

if [ "${FILES_CHANGED}" -le "${LITE_MAX_FILES}" ] && [ "${LINES_CHANGED}" -le "${LITE_MAX_LINES}" ]; then
  echo "TIER=lite"
  echo "REASON=${LINES_CHANGED} lines / ${FILES_CHANGED} files (<= lite threshold)"
  exit 0
fi

echo "TIER=full"
echo "REASON=${LINES_CHANGED} lines / ${FILES_CHANGED} files (exceeds lite threshold)"
