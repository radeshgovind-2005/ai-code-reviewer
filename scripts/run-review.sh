#!/usr/bin/env bash
set -euo pipefail

# Expects these to already be set by the workflow:
#   GITHUB_TOKEN      - for the GitHub API (gh CLI): posting the PR review
#   GITHUB_REPOSITORY - "owner/repo" (set automatically by Actions)
#   LLM_API_KEY       - the model provider key
#   PR_NUMBER         - pull request number
#   BASE_SHA          - base commit sha
#   HEAD_SHA          - head commit sha
#   REVIEWER_HOME     - path to this reviewer repo checkout

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

REVIEW_SUMMARY_EXTRA=""

# Config resolution mirrors tier-pr.sh: consuming repo's file wins.
if [ -f "review-config.json" ]; then
  CONFIG_FILE="review-config.json"
else
  CONFIG_FILE="${REVIEWER_HOME}/review-config.json"
fi
# Whether the bot may submit APPROVE reviews. If your branch protection
# counts github-actions approvals, this is what lets PRs merge -- set it to
# false to require a human approval on every PR.
BOT_CAN_APPROVE="$(jq -r 'if .bot_can_approve == false then "false" else "true" end' "${CONFIG_FILE}")"
BOT_LOGIN="${BOT_LOGIN:-github-actions[bot]}"
REVIEWS_API="repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}/reviews"

# Dismiss this bot's earlier APPROVED / CHANGES_REQUESTED reviews so re-runs
# and new pushes don't leave a stale verdict (e.g. an old approval) standing.
# COMMENTED reviews can't be dismissed via the API; they're left as history.
dismiss_previous_reviews() {
  local ids
  ids="$(gh api "${REVIEWS_API}" --paginate \
    --jq ".[] | select(.user.login == \"${BOT_LOGIN}\" and (.state == \"APPROVED\" or .state == \"CHANGES_REQUESTED\")) | .id" \
    2>/dev/null || true)"
  for id in ${ids}; do
    gh api --method PUT "${REVIEWS_API}/${id}/dismissals" \
      -f message="Superseded by a newer AI review of ${HEAD_SHA:0:7}." > /dev/null \
      || echo "warning: could not dismiss previous review ${id}" >&2
  done
}

post_review() {
  local payload_file="$1"
  dismiss_previous_reviews
  gh api "${REVIEWS_API}" --method POST --input "${payload_file}" > /dev/null
}

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
      if [ -n "${REVIEW_SUMMARY_EXTRA}" ]; then
        echo "| review | \`${REVIEW_SUMMARY_EXTRA}\` |"
      fi
      echo "| time (UTC) | $(date -u +'%Y-%m-%dT%H:%M:%SZ') |"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  echo "tier=${TIER} reason=\"${REASON}\" model=${MODEL_NAME} status=${status} ${REVIEW_SUMMARY_EXTRA}"
}

# 3. Trivial changes: skip the model call entirely, just log it.
# Still post a review, otherwise a required-approval rule leaves the PR
# stuck with no signal at all.
if [ "${TIER}" = "trivial" ]; then
  TRIVIAL_EVENT="COMMENT"
  [ "${BOT_CAN_APPROVE}" = "true" ] && TRIVIAL_EVENT="APPROVE"
  TRIVIAL_PAYLOAD="$(mktemp)"
  jq -n --arg sha "${HEAD_SHA}" --arg ev "${TRIVIAL_EVENT}" --arg reason "${REASON}" \
    '{commit_id: $sha, event: $ev,
      body: ("### Summary\nTrivial change (" + $reason + ") -- AI review skipped by tiering.")}' \
    > "${TRIVIAL_PAYLOAD}"
  post_review "${TRIVIAL_PAYLOAD}"
  REVIEW_SUMMARY_EXTRA="event=${TRIVIAL_EVENT}"
  log_summary "skipped model (trivial tier), review posted"
  exit 0
fi

# 4. Build the prompt: reviewer instructions + tier note + a line-numbered
# diff (so findings can cite real, verifiable line numbers -- see
# annotate-diff.py / post-review.py).
ANNOTATED_DIFF_FILE="$(mktemp)"
python3 "${REVIEWER_HOME}/scripts/annotate-diff.py" < "${DIFF_FILE}" > "${ANNOTATED_DIFF_FILE}"

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
cat "${ANNOTATED_DIFF_FILE}" >> "${PROMPT_FILE}"
echo '```' >> "${PROMPT_FILE}"
echo "" >> "${PROMPT_FILE}"
echo "Review the diff above per your instructions." >> "${PROMPT_FILE}"

# 5. Run OpenCode non-interactively. Pass the whole prompt as a single
# positional argument -- combining -f with a trailing instruction string
# caused OpenCode's CLI to misparse the instruction as a second filename.
REVIEW_OUTPUT="$(opencode run "$(cat "${PROMPT_FILE}")")"

# 6. Turn the markdown output into a real GitHub PR Review (inline comments
# anchored to real diff lines + an event that can actually gate the merge),
# not a plain issue comment that GitHub has no way to act on.
PAYLOAD_FILE="$(mktemp)"
APPROVE_FLAG=""
[ "${BOT_CAN_APPROVE}" = "true" ] || APPROVE_FLAG="--no-approve"
POST_REVIEW_STDERR="$(mktemp)"
echo "${REVIEW_OUTPUT}" \
  | python3 "${REVIEWER_HOME}/scripts/post-review.py" --diff "${DIFF_FILE}" --commit "${HEAD_SHA}" ${APPROVE_FLAG} \
  > "${PAYLOAD_FILE}" 2> "${POST_REVIEW_STDERR}"
REVIEW_SUMMARY_EXTRA="$(cat "${POST_REVIEW_STDERR}")"

post_review "${PAYLOAD_FILE}"

log_summary "posted"
