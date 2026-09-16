# ai-code-reviewer

Automated pull-request review for any GitHub repo: **free deterministic scanners** plus **specialist AI reviewers** (security, correctness, performance, tests, docs, your own engineering standards) and a **coordinator** that merges, verifies and decides. Built on [OpenCode](https://opencode.ai/), shipped as a reusable GitHub workflow. Inspired by Cloudflare's [AI code review](https://blog.cloudflare.com/ai-code-review/) and [standards enforcement](https://blog.cloudflare.com/engineering-standards-enforcement/) posts, scaled down to one workflow.

```
PR ──► Scanners job ─ gitleaks · osv-scanner · opengrep · trivy · zizmor · actionlint ─► findings.json ─┐
  └──► AI Review job                                                                                    │
         config (base branch) → diff → tier → context (AGENTS.md, PR text, standards, coverage, ◄───────┘
                                                        previous threads, scanner findings)
           → reviewers in parallel, each with its own model ─► coordinator ─► one GitHub PR review
           → any failure: "review did not complete" + red job (fail closed)
```

## Quickstart

1. **Add the secret** in the repo you want reviewed: *Settings → Secrets and variables → Actions → `LLM_API_KEY`* (an Anthropic key for the default config; see [Models](#models--byok)). No key = free-tier model, never approves.
2. **Add the workflow** — copy [`examples/consumer/pr-review.yml`](examples/consumer/pr-review.yml) to `.github/workflows/pr-review.yml`. Minimal version:

   ```yaml
   name: AI PR Review
   on:
     pull_request:
       types: [opened, synchronize, reopened, ready_for_review]
   permissions: {}
   jobs:
     review:
       permissions:
         contents: read
         pull-requests: write
       uses: radeshgovind-2005/ai-code-reviewer/.github/workflows/review.yml@v2
       with:
         reviewer_ref: v2   # keep in sync with the @ref above
       secrets:
         LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
   ```
3. **Protect the branch** so the review actually gates merges — see [docs/SETUP.md](docs/SETUP.md).
4. **Try it on a seeded bad PR:** `scripts/make-playground-pr.sh` in a test repo, push, open a PR.

## What runs

### Scanners (free, deterministic, pinned + checksum-verified)

| Tool | Looks at | Blocks by default |
|---|---|---|
| gitleaks | the PR's commits (secrets) | any secret |
| osv-scanner | dependency manifests / lockfiles | high/critical CVE, if the PR changed that manifest |
| opengrep + pinned opengrep-rules | changed source files, rules by language | never (reported) |
| trivy config | changed Dockerfile / Terraform / k8s / compose | critical |
| zizmor | changed `.github/workflows/*` | high |
| actionlint | changed `.github/workflows/*` | never (reported) |

Findings block only when **introduced by the PR**. The Scanners job goes red on blocking findings; the AI review lists all of them, won't repeat them, and turns blocking ones into `REQUEST_CHANGES`. A scanner that crashes or can't reach its API is reported as `failed` and doesn't block (`scanners.fail_on_error: true` to change).

### AI reviewers (model by responsibility)

| Reviewer | Owns | Default model | Tiers | Runs when |
|---|---|---|---|---|
| security | exploitable issues only | Sonnet 4.6 → 4.5 | trivial, lite, full | always |
| correctness | bugs, error handling, broken contracts | Sonnet 4.6 → 4.5 | trivial, lite, full | always |
| performance | measurable regressions | Sonnet 4.6 → 4.5 | full | always |
| tests | changed behaviour has tests (+ changed-line coverage) | Haiku 4.5 → Sonnet 4.5 | lite, full | always |
| docs | docs / README / AGENTS.md drift | Haiku 4.5 → Sonnet 4.5 | lite, full | docs-like files changed |
| standards | your `.review/standards/*.md` | Sonnet 4.6 → 4.5 | lite, full | a standard matches a changed path |
| **coordinator** | dedupe, verify against code, calibrate, verdict, resolve fixed threads | Opus 4.7 → Sonnet | lite, full | — (trivial: deterministic merge) |

**Tier decides which reviewers run; each reviewer has its own model.** Tiers come from diff size (lockfiles/generated files don't count) and sensitive paths:
trivial ≤ 10 lines & ≤ 2 files · lite ≤ 100 lines & ≤ 10 files · full otherwise or any sensitive path.

Reviewers are read-only OpenCode agents (read/grep/glob the checkout; no shell, no edits, no network tools), run in parallel with retries on 429/5xx, fallback models, a heartbeat, an inactivity kill and an overall deadline.

### Review events

| Situation | Event |
|---|---|
| Any `critical` finding, `changes_requested`, or a blocking scanner finding | `REQUEST_CHANGES` |
| Warnings/suggestions, malformed/unparseable output | `COMMENT` |
| All clean | `APPROVE` |
| Sensitive path · partial (truncated) diff · a reviewer or the coordinator failed · free tier · `bot_can_approve: false` | never `APPROVE` (says why) |
| Nothing could be reviewed (all reviewers failed, crash, setup failure) | `COMMENT` "did not complete" + job fails |

On new pushes the bot dismisses its old approval / change request, shows the coordinator its previous threads (with replies), **resolves threads that were fixed**, and doesn't repost findings that are still open.

Every review ends with a footer like `4 reviewer(s): security, correctness, tests, docs + coordinator · 1m42s · 58.3k tokens · $0.21`; the job summary has per-reviewer model, attempts, time, tokens and cost, and the `ai-review` artifact has the exact prompts.

## Configuration

`review-config.json` in your repo, read from the **base branch** and deep-merged over [the defaults](review-config.json):

| Key | Default | Meaning |
|---|---|---|
| `bot_can_approve` | `true` | `false` → never approves |
| `max_diff_bytes` | `400000` | larger diffs are cut per file → partial, non-approving review |
| `diff_excludes` | `[]` | extra globs to ignore (e.g. `"generated/**"`); migrations are never excluded |
| `sensitive_paths` | `auth/`, `payments/`, `secrets/`, `.github/workflows/`, `review-config.json`, `*.pem`, `*.key` | never auto-approved, always full tier |
| `thresholds.trivial` / `.lite` | 10/2 · 100/10 | lines / files |
| `agents.<name>` | see table | `model`, `fallback[]`, `tiers[]`, `paths[]`, `enabled`, `timeout` |
| `coordinator` | Opus 4.7 | `model`, `fallback[]`, `tiers[]`, `enabled` |
| `review` | | `max_parallel`, `timeout`, `overall_timeout`, `inactivity_timeout`, `retries`, `resolve_fixed_threads`, `free_tier_model` |
| `scanners.<tool>` | see table | `enabled`, `block_on` (`none\|low\|medium\|high\|critical`), `ignore_rules[]`; `fail_on_error` |

Path patterns (sensitive paths, agent `paths`, standards): `auth/` = that directory at any depth · `Dockerfile` = that name at any depth · `*.pem` / `**/x` = glob on the full path.

Workflow inputs: `reviewer_ref`, `opencode_version` (pinned), `model_timeout`, `scanners` (bool), `coverage_artifact` + `coverage_path`.

### Repository context the reviewers get

- **`AGENTS.md`** (or `.review/instructions.md`) from the base branch — conventions a diff can't show. [Example](examples/consumer/AGENTS.md).
- **Engineering standards** in `.review/standards/*.md` from the base branch ([examples](examples/consumer/.review/standards)):
  ```markdown
  ---
  id: no-swallowed-exceptions
  level: MUST          # MUST | SHOULD
  status: enforced     # approved = reported only, enforced = MUST blocks
  paths: ["*.py"]
  ---
  Catching an exception must handle it, re-raise it, or log it with context.
  ```
  Only standards matching changed files are sent. `MUST`+`enforced` findings become critical; everything else is capped at warning — enforced in code, not left to the model.
- **PR title/description**, **scanner findings**, **previous threads** — as untrusted, nonce-wrapped data.
- **Changed-line coverage** — pass a coverage artifact; diff-cover gives the tests reviewer the uncovered changed lines.

## Models / BYOK

Models are OpenCode `provider/model` ids. `LLM_API_KEY` is exported as the key variable of every provider your config uses (`anthropic` → `ANTHROPIC_API_KEY`, `openai`, `openrouter`, `groq`, `google`, `deepseek`, `xai`, `mistral`). To mix providers, pass provider-specific secrets as env instead. With no key at all, every reviewer uses `review.free_tier_model` and the bot never approves.

## Security model

- **Fails closed** — a broken run never looks like a pass.
- **Rules come from the base branch** — `review-config.json`, `.review/standards`, `AGENTS.md`, `.gitleaks.toml`/`.gitleaksignore`. A PR can't loosen what it's judged by.
- **The PR can't configure the reviewer.** OpenCode runs with project config, plugins, `AGENTS.md`/`CLAUDE.md` auto-loading disabled, a read-only agent, and an environment without GitHub tokens. (Before v2 a PR could ship an OpenCode plugin and run code with your keys — pin `@v2`.)
- **Untrusted text is data** — diff, PR description, scanner paths, thread replies are wrapped in markers with a random id and the prompt says never to follow instructions inside.
- **Pinned everything** — actions by SHA, scanners by version + sha256, OpenCode by npm version, reviewer by tag.

## Local use

```bash
scripts/review-local.sh --base main          # AI review of committed + uncommitted changes, printed
scripts/review-local.sh --scan               # + scanners (install: scripts/install-scanners.sh)
python3 evals/run.py --only sql-injection    # score the reviewers on seeded PRs (needs a key)
pre-commit install                           # examples/consumer/pre-commit-config.yaml: fast checks before push
```

## Quality: evals

`evals/cases/*.json` are seeded PRs with expected findings (must-find, false-positive budget, allowed verdicts). `python3 evals/run.py` runs the real pipeline and reports recall, false positives per case, verdict accuracy, p50/p95 time and cost. Run it before changing prompts, models or tiers and compare with the baseline in the roadmap. When the reviewer misses something on a real PR, add a case.

## Repo layout

```
.github/workflows/review.yml   reusable workflow (Scanners + AI Review jobs)
.github/workflows/ci.yml       tests, lint, real-scanner and real-OpenCode integration tests
.github/workflows/self-review.yml   this repo's PRs reviewed by their own code
agents/                        _shared.md + one prompt per reviewer + coordinator.md
opencode.json                  read-only `ai-reviewer` agent
review-config.json             defaults (tiers, agents/models, scanners, limits)
scripts/run-review.sh          AI Review job: config → diff → tier → review.py → post
scripts/review.py, reviewer/   orchestrator: selection, prompts, OpenCode runner, coordinator, standards
scripts/post-review.py         final JSON → GitHub review payload
scripts/run-scanners.sh        Scanners job; normalize-scanners.py → findings/markdown/SARIF
scripts/install-scanners.sh    pinned + checksummed scanner install
scripts/review-local.sh        local dry run
scripts/make-playground-pr.sh  seeded bad PR for demos
evals/                         eval cases + harness
examples/                      consumer workflow, standards, AGENTS.md, pre-commit, playground
tests/                         unit + end-to-end tests (fake gh/opencode/scanners)
```

## Development

```bash
python3 -m unittest discover -s tests            # ~100 tests, no network
SCANNERS_INTEGRATION=1 python3 -m unittest tests.test_scanners_integration   # real scanners
OPENCODE_INTEGRATION=1 python3 -m unittest tests.test_opencode_integration   # real opencode + mock model
shellcheck -S warning scripts/*.sh && ruff check scripts tests evals
```

## Limitations

- Not a replacement for human review: limited architectural and cross-service awareness, weak on subtle concurrency.
- Cost grows with diff size and tier; very large PRs get partial reviews.
- Model ids in the defaults must exist for your provider — check the job summary's per-reviewer status after your first run and override in `review-config.json` if needed.

## License

MIT
