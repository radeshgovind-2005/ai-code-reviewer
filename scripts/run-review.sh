#!/usr/bin/env bash
set -euo pipefail

# Expects these to already be set by the workflow:
#   GITHUB_TOKEN   - for posting the PR comment (gh CLI)
#   LLM_API_KEY    - the model provider key
#   PR_NUMBER      - pull request number
#   BASE_SHA       - base commit sha
#   HEAD_SHA       - head commit sha

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# v0: single provider, hardcoded to Anthropic (matches opencode.json).
# v3 will generalize this to resolve provider dynamically.
export ANTHROPIC_API_KEY="${LLM_API_KEY}"

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

# 2. Build the prompt: reviewer instructions + diff.
PROMPT_FILE="$(mktemp)"
cat "${REVIEWER_HOME:?REVIEWER_HOME not set}/agents/reviewer.md" > "${PROMPT_FILE}"
echo "" >> "${PROMPT_FILE}"
echo "## Diff to review" >> "${PROMPT_FILE}"
echo '```diff' >> "${PROMPT_FILE}"
cat "${DIFF_FILE}" >> "${PROMPT_FILE}"
echo '```' >> "${PROMPT_FILE}"

# 3. Run OpenCode non-interactively.
REVIEW_OUTPUT="$(opencode run -f "${PROMPT_FILE}" "Review the diff above per your instructions.")"

# 4. Post as a single PR comment.
echo "${REVIEW_OUTPUT}" | gh pr comment "${PR_NUMBER}" --body-file -