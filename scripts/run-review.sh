#!/usr/bin/env bash
set -euo pipefail

# Expects these to already be set by the workflow:
#   GITHUB_TOKEN   - for posting the PR comment (gh CLI)
#   LLM_API_KEY    - the model provider key
#   PR_NUMBER      - pull request number
#   BASE_SHA       - base commit sha
#   HEAD_SHA       - head commit sha
#   REVIEWER_HOME  - path to this reviewer repo checkout

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# v0: single provider, hardcoded to Anthropic (matches opencode.json).
# v3 will generalize this to resolve provider dynamically.
export ANTHROPIC_API_KEY="${LLM_API_KEY}"
MODEL_NAME="$(jq -r '.model' "${REVIEWER_HOME:?REVIEWER_HOME not set}/opencode.json")"

# 1. Get the diff, filtering out noise.
DIFF_FILE="$(mktemp)"
git diff "${BASE_SHA}...${HEAD_SHA}" \
  -- . \
  ':(exclude)*.lock' \
  ':(exclude)package-lock.json' \
  ':(exclude)yarn.lock' \
  ':(exclude)pnpm-lock.yaml' \
  ':(exclude)*.min.js' \
  ':(exclude)*.min.css' \
  ':(exclude)dist/**' \
  ':(exclude)vendor/**' \
  > "${DIFF_FILE}"

if [ ! -s "${DIFF_FILE}" ]; then
  echo "No reviewable changes after filtering. Skipping."
  exit 0
fi

# 2. Tier the PR from diff stats alone -- no model call yet (v1).
TIER_OUTPUT="$("${REVIEWER_HOME}/scripts/tier-pr.sh")"
TIER="$(echo "${TIER_OUTPUT}" | grep '^TIER=' | cut -d= -f2)"
REASON="$(echo "${TIER_OUTPUT}" | grep '^REASON=' | cut -d= -f2-)"

log_summary() {
  local status="$1"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "### AI Review"
      echo ""
      echo "| field | value |"
      echo "|---|---|"
      echo "| tier | \`${TIER}\` |"
      echo "| reason | ${REASON} |"
      echo "| model | \`${MODEL_NAME}\` |"
      echo "| status | ${status} |"
      echo "| time (UTC) | $(date -u +'%Y-%m-%dT%H:%M:%SZ') |"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  echo "tier=${TIER} reason=\"${REASON}\" model=${MODEL_NAME} status=${status}"
}

# 3. Trivial changes: skip the model call entirely, just log it.
if [ "${TIER}" = "trivial" ]; then
  log_summary "skipped (trivial tier)"
  exit 0
fi

# 4. Build the prompt: reviewer instructions + tier note + diff.
PROMPT_FILE="$(mktemp)"
cat "${REVIEWER_HOME}/agents/reviewer.md" > "${PROMPT_FILE}"
echo "" >> "${PROMPT_FILE}"
if [ "${TIER}" = "lite" ]; then
  echo "## Tier note" >> "${PROMPT_FILE}"
  echo "This PR was classified as **lite** tier (${REASON}). Keep the review brief -- focus only on the highest-severity findings." >> "${PROMPT_FILE}"
  echo "" >> "${PROMPT_FILE}"
fi
echo "## Diff to review" >> "${PROMPT_FILE}"
echo '```diff' >> "${PROMPT_FILE}"
cat "${DIFF_FILE}" >> "${PROMPT_FILE}"
echo '```' >> "${PROMPT_FILE}"
echo "" >> "${PROMPT_FILE}"
echo "Review the diff above per your instructions." >> "${PROMPT_FILE}"

# 5. Run OpenCode non-interactively. Pass the whole prompt as a single
# positional argument -- combining -f with a trailing instruction string
# caused OpenCode's CLI to misparse the instruction as a second filename.
REVIEW_OUTPUT="$(opencode run "$(cat "${PROMPT_FILE}")")"

# 6. Post as a single PR comment.
echo "${REVIEW_OUTPUT}" | gh pr comment "${PR_NUMBER}" --body-file -

log_summary "posted"
