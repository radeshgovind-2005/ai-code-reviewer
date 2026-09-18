#!/usr/bin/env bash
set -euo pipefail

# Creates a branch with the seeded bad PR from examples/playground in the git
# repository you run it from, so you can open a PR and watch the Scanners and
# AI Review jobs catch everything.
#
#   cd ~/dev/my-test-repo
#   ~/dev/ai-code-reviewer/scripts/make-playground-pr.sh [branch-name]
#   git push -u origin <branch-name>   # then open a PR
#
# Use a throwaway/test repository: it commits a fake secret and an insecure
# workflow on purpose (the workflow only triggers on pull_request_target from
# that branch's own PRs; delete the branch when you're done).

REVIEWER_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${1:-ai-review-playground-$(date +%Y%m%d%H%M%S)}"
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain)" ]; then
  echo "error: working tree not clean" >&2
  exit 1
fi
git checkout -b "${BRANCH}"
(cd "${REVIEWER_HOME}/examples/playground" && find . -type f -name '*.seed') | while IFS= read -r f; do
  dest="${f%.seed}"
  mkdir -p "$(dirname "${dest}")"
  cp "${REVIEWER_HOME}/examples/playground/${f}" "${dest}"
done
git add -A
git commit -qm "playground: seeded problems for the AI reviewer"
echo "Created branch ${BRANCH}. Push it and open a PR:"
echo "  git push -u origin ${BRANCH}"
