# Setting up a repository

## 1. Secret

*Settings → Secrets and variables → Actions → New repository secret* → `LLM_API_KEY`.

The default config uses Anthropic models (`anthropic/claude-sonnet-4-6`, `claude-haiku-4-5`, `claude-opus-4-7`). Using another provider: override `agents.*.model` / `coordinator.model` in `review-config.json` on your default branch. No secret at all → free-tier model, the bot never approves.

## 2. Workflow

Copy [`examples/consumer/pr-review.yml`](../examples/consumer/pr-review.yml). Pin both the `uses:` ref and `reviewer_ref` to the same tag.

## 3. Branch protection (this is what makes it a gate)

*Settings → Branches → Add rule* (or a ruleset) for your default branch:

- **Require a pull request before merging**
  - *Require approvals*: 1. If the bot's approval should count, leave "Dismiss stale approvals" on and keep `bot_can_approve: true`; if a human must always approve, set `"bot_can_approve": false`.
  - *Require review from Code Owners* for sensitive areas if you use CODEOWNERS.
- **Require status checks to pass**: add
  - `review / Scanners` (blocks secrets, new high/critical CVEs, dangerous workflows)
  - `review / AI Review` (fails when the review could not complete)
  - (check names show as `<caller job id> / <job name>`; pick them from the list after the first run)
- **Require conversation resolution before merging** — findings are review threads; the bot resolves the ones that get fixed.
- Optionally **Require branches to be up to date**.

`REQUEST_CHANGES` from the bot blocks merging only while "Require approvals" is on and the review isn't dismissed; the bot dismisses its own old change requests when a new push is clean.

## 4. Trade-offs to decide

| Decision | Default | Consider changing when |
|---|---|---|
| Bot can approve | yes, except sensitive paths / partial reviews / failed reviewers / free tier | regulated code, small teams wanting a human on every PR → `bot_can_approve: false` |
| Trivial PRs | still reviewed by security + correctness | cost-sensitive monorepos → raise `thresholds.trivial` carefully |
| Scanner blocking | secrets, high CVE (changed manifest), critical IaC, high workflow issues | noisy rules → `scanners.<tool>.ignore_rules`; stricter → lower `block_on` |
| Scanners failing open | a crashed scanner doesn't block | you need guaranteed coverage → `scanners.fail_on_error: true` |
| Re-review resolves threads | yes | humans want to resolve everything → `review.resolve_fixed_threads: false` |

## 5. Forks

`pull_request` workflows from forks get no secrets and a read-only token: the AI review falls back to the free model and can't post (the job fails). If you accept outside contributions, either require a maintainer to re-run on a branch in the repo, or keep the check non-required for fork PRs. **Do not** switch to `pull_request_target` with a checkout of the PR head — that hands your secrets to the PR (zizmor will flag it).

## 6. Code scanning (optional)

The Scanners job uploads `scanner-findings.sarif` as an artifact. With GitHub code scanning (free for public repos, GHAS for private) you can add, in your own workflow after the review job:

```yaml
  upload-sarif:
    needs: review
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    permissions:
      security-events: write
      actions: read
    steps:
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: scanner-findings
      - uses: github/codeql-action/upload-sarif@v4
        with:
          sarif_file: scanner-findings.sarif
          category: ai-code-reviewer
```

SonarCloud (free for public repos) and CodeQL are good add-ons; they aren't bundled.

## 7. Tuning quality

1. Add `AGENTS.md` with conventions a reviewer can't infer from a diff.
2. Encode team rules as `.review/standards/*.md` — start `status: approved` (comments only), switch to `enforced` once the false-positive rate is acceptable.
3. When the reviewer misses or invents something on a real PR, add an eval case and run `python3 evals/run.py` before and after changing prompts/models.
