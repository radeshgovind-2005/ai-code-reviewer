# ai-code-reviewer

AI-powered code review for pull requests, built on [OpenCode](https://opencode.ai/). Drop-in reusable GitHub workflow — plug it into any repo and every PR gets an automatic review comment.

## What it does

- Runs on every pull request
- Pulls the diff, sends it to an LLM with a scoped review prompt
- Posts a single structured comment: what's good, what's risky, what to fix
- Free and open — bring your own model or point it at a free-tier one

## Why

Human review is great but slow. This catches the obvious stuff (bugs, missing error handling, style drift, security smells) automatically, so human reviewers can focus on the things that actually need judgment.

## Quickstart

**1. Get an API key** (or use a free-tier model — see [Model options](#model-options))

**2. In this repo**, set your key as a secret:
```
Settings → Secrets and variables → Actions → New repository secret
Name: LLM_API_KEY
```

**3. In any repo you want reviewed**, add:

`.github/workflows/pr-review.yml`
```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  review:
    uses: radeshgovind-2005/ai-code-reviewer/.github/workflows/review.yml@v1
    with:
      reviewer_ref: v1   # keep in sync with the @ref above
    secrets:
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
```

Open a PR — it gets reviewed automatically.

## Repo structure

```
ai-code-reviewer/
├── .github/workflows/
│   ├── review.yml             # the reusable workflow other repos call
│   └── ci.yml                 # tests for this repo
├── agents/reviewer.md         # what to flag / not flag + JSON output contract
├── review-config.json         # tier thresholds, sensitive paths, bot_can_approve
├── opencode.json              # model + provider config
├── scripts/
│   ├── run-review.sh          # orchestrates: diff → tier → prompt → model → post review
│   ├── tier-pr.sh             # trivial / lite / full from diff stats
│   ├── annotate-diff.py       # prefixes diff lines with real line numbers
│   └── post-review.py         # model JSON → GitHub PR review payload
└── tests/
    ├── test_post_review.py
    └── fixtures/              # sample diff + recorded model outputs
```

## Development

```
python3 -m unittest discover -s tests -v
```

Every tricky model output (bad JSON, wrong line numbers, contradicting verdicts, the old markdown format) lives as a file in `tests/fixtures/outputs/` with its expected event in `CASES`. When the reviewer misbehaves on a real PR, save the model output there first, then fix.

## Model options

| Option | Cost | Notes |
|---|---|---|
| Anthropic / OpenAI API | Pay per token | Cheap at this scale — usually cents per review |
| Groq free tier | Free | Fast, generous free limits |
| Ollama (self-hosted) | Free | Runs locally, no API key needed |
| Cloudflare Workers AI | Free tier | Good if you're already on Cloudflare |

Change the model in `opencode.json` — nothing else needs to change.

## Customizing what it checks

Edit `agents/reviewer.md`. Be explicit about what **not** to flag — this is the single biggest lever for review quality. Vague prompts produce noisy, low-trust reviews.

## Review events & approvals

The model must answer with a JSON object (schema in `agents/reviewer.md`). The bot turns it into a real PR review, not a plain comment:

| Situation | Event |
|---|---|
| Any `critical` finding, or verdict "changes requested" | `REQUEST_CHANGES` |
| Warnings/suggestions, malformed findings, legacy markdown output, or output it can't parse | `COMMENT` |
| Valid JSON, no findings, verdict `approve` | `APPROVE` |
| Trivial tier (model skipped) | `APPROVE` |
| PR touches a sensitive path | never `APPROVE` (clean → `COMMENT`) |
| Model error / timeout / empty output, parser crash, any script error | `COMMENT` saying the review didn't complete + job fails |

Before each post, the bot dismisses its own earlier `APPROVED` / `CHANGES_REQUESTED` reviews so a stale verdict never stays in place.

If your branch protection counts `github-actions` approvals, the bot's approval is enough to merge. To always require a human, set `"bot_can_approve": false` in `review-config.json` (then `APPROVE` becomes `COMMENT`).

## Security model

- **Fails closed.** If anything breaks after the PR is identified, the bot posts a "review did not complete" comment, dismisses its old approval, and the job goes red. A broken run never looks like a pass.
- **Config comes from the base branch.** `review-config.json` is read from the PR's base commit, so a PR can't empty `sensitive_paths` or raise thresholds to approve itself. Changes to `review-config.json` are themselves a sensitive path.
- **Sensitive paths need a human.** The bot never approves them.
- **The diff is treated as data.** It's wrapped in markers with a random id the author can't predict, and the prompt tells the model to ignore (and flag) instructions inside it. This reduces prompt injection; it doesn't eliminate it -- which is why approval is also gated by the rules above.
- **Pin a version.** Use `@v1` + `reviewer_ref: v1` rather than `@main` so changes here don't silently change your gate.

## Limitations

- Not a replacement for human review — it misses architectural context and cross-system impact
- Best on small-to-medium diffs; huge refactors get expensive and less accurate
- Only as good as the prompt in `agents/reviewer.md` — tune it for your codebase

## License

MIT — free to use, fork, and modify.