# shellcheck shell=bash
# Shared helpers for run-review.sh and run-scanners.sh. Source, don't execute.

# resolve_config OUT_FILE
# Reads review-config.json from the BASE commit (so a PR can't loosen its own
# rules) and deep-merges it over this reviewer's defaults. Returns 1 if the
# consumer's file isn't valid JSON.
resolve_config() {
  local out="$1" consumer
  consumer="$(mktemp)"
  if git cat-file -e "${BASE_SHA}:review-config.json" 2>/dev/null; then
    git show "${BASE_SHA}:review-config.json" > "${consumer}"
    if ! jq -s '.[0] * .[1]' "${REVIEWER_HOME}/review-config.json" "${consumer}" > "${out}"; then
      rm -f "${consumer}"
      return 1
    fi
  else
    cp "${REVIEWER_HOME}/review-config.json" "${out}"
  fi
  rm -f "${consumer}"
}

# load_excludes -> fills global EXCLUDES array from scripts/diff-excludes.txt
# plus the config's "diff_excludes" globs (e.g. "generated/**"), if CONFIG_FILE
# is set. Database migrations are never excluded, whatever the config says.
load_excludes() {
  EXCLUDES=()
  local p
  while IFS= read -r p; do
    [[ -z "$p" || "$p" == \#* ]] && continue
    EXCLUDES+=("$p")
  done < "${REVIEWER_HOME}/scripts/diff-excludes.txt"
  if [ -n "${CONFIG_FILE:-}" ] && [ -f "${CONFIG_FILE}" ]; then
    while IFS= read -r p; do
      [ -z "$p" ] && continue
      [[ "$p" == *migration* ]] && continue
      EXCLUDES+=(":(exclude,glob)${p}")
    done < <(jq -r '(.diff_excludes // [])[] | strings' "${CONFIG_FILE}")
  fi
}
