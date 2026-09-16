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
    uses: radeshgovind-2005/ai-code-reviewer/.github/workflows/review.yml@main
    secrets:
      LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
```

Open a PR — it gets reviewed automatically.

## Repo structure

```
ai-code-reviewer/
├── .github/
│   └── workflows/
│       └── review.yml        # the reusable workflow other repos call
├── opencode.json              # model + provider config
├── agents/
│   └── reviewer.md            # what to flag / not flag
├── scripts/
│   └── run-review.sh          # fetches diff, runs opencode, posts PR comment
└── README.md
```

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

The bot posts a real PR review, not a plain comment:

| Situation | Event |
|---|---|
| Any `critical` finding, or verdict "changes requested" | `REQUEST_CHANGES` |
| Warnings/suggestions, unparsed notes, or output it can't parse | `COMMENT` |
| "No issues found." + verdict "approve" | `APPROVE` |
| Trivial tier (model skipped) | `APPROVE` |

Before each post, the bot dismisses its own earlier `APPROVED` / `CHANGES_REQUESTED` reviews so a stale verdict never stays in place.

If your branch protection counts `github-actions` approvals, the bot's approval is enough to merge. To always require a human, set `"bot_can_approve": false` in `review-config.json` (then `APPROVE` becomes `COMMENT`).

## Limitations

- Not a replacement for human review — it misses architectural context and cross-system impact
- Best on small-to-medium diffs; huge refactors get expensive and less accurate
- Only as good as the prompt in `agents/reviewer.md` — tune it for your codebase

## License

MIT — free to use, fork, and modify.