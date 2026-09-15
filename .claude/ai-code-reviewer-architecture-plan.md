# ai-code-reviewer — Architecture Plan

Reference: [Cloudflare AI Code Review](https://blog.cloudflare.com/ai-code-review/) · [Engineering Standards Enforcement](https://blog.cloudflare.com/engineering-standards-enforcement/)

Repo: `radeshgovind-2005/ai-code-reviewer` — reusable GitHub Actions workflow, built on OpenCode, that any repo can plug in for automatic PR review.

## Core principle

Keep the *shape* of the pipeline from v0, even when each stage is minimal:

```
config → tiering → prompt-assembly → invoke model → post comment
```

Every later phase below is additive to this shape, not a rewrite.

## Phased build order

Maps to the current scaffold: `.github/workflows/review.yml`, `opencode.json`, `agents/reviewer.md`, `scripts/run-review.sh`.

- **v0 — single agent, single tier**
  `run-review.sh`: diff → one prompt → one model call → one PR comment. Shippable on its own.

- **v1 — tiering**
  Add a cheap pre-step that classifies the PR (lines changed, files changed, touches sensitive paths like `auth/`, `payments/`) into **trivial / lite / full** *before* calling the model. Only changes which prompt template and which model tier get used. No new agents yet.

- **v2 — multi-section review + coordinator**
  Split `reviewer.md` into focused sections (security, quality, docs, ...) and a coordinator step that assembles/dedupes findings. Don't spawn N concurrent processes like Cloudflare does — that solves for their scale, not this project's. One call with a structured, multi-section prompt gets most of the value far more simply. Real concurrent orchestration is a later optimization if cost/latency ever demand it.

- **v3 — provider abstraction (BYOK)**
  `opencode.json` becomes a provider map instead of a single model string. This is where bring-your-own-key support lives (see below).

- **v4 — plugins**
  Only once there are ≥2 real use cases for "someone wants to inject their own check." A plugin = a prompt-fragment + a hook-point registration. No need for Cloudflare's `ConfigureContext` isolation layer — that solves for many teams sharing one system, not this.

## BYOK / key safety

- This is a **reusable GitHub workflow** — the workflow itself never touches anyone's key.
- Each consuming repo stores `LLM_API_KEY` as its own repo/org secret and passes it in via `secrets:` in the caller workflow. Never logged, never written to an uploaded artifact, never phoned home.
- **Zero-config default**: wire up OpenCode's free-tier account so `clone → open PR → get a review` works with no key at all.
- Resolve provider at runtime in `opencode.json`: `LLM_API_KEY` present → use it; absent → fall back to free tier. That's the entire "safe for others to self-serve" story — no shared account, no key storage, no proxy.

## Tiering logic (trivial / lite / full)

- Computed from diff stats only — no model call needed. Lines changed, files changed, whether it touches a configurable "sensitive paths" list.
- Thresholds should be tuned much lower than Cloudflare's (this is a solo/small-scale project, not thousands of MRs/day).
- Tier controls: how many prompt sections get included, and optionally which model tier is used (e.g. a lighter/free model for trivial, a stronger one for full) — this is the main cost lever.

## Agents & coordinator, scaled down

- Skip literal "spawn 7 child processes" — that exists for Cloudflare's concurrency needs at scale.
- One process, one call, structured prompt sections (security / quality / docs as sections, not separate spawns) gets ~80% of the value with near-zero added complexity.
- Keep their best idea regardless of scale: **be explicit about what NOT to flag** in each prompt section — the single biggest lever on review quality per the blog.

## Plugins

- Don't build a plugin system until there are two real things that need to plug in.
- When the time comes: a `plugins/` folder + a simple array in `opencode.json` (e.g. `hooks: ["plugins/my-check.js"]`) is enough.

## Open decisions for later sessions

- [ ] Exact tier thresholds (lines/files) for trivial vs. lite vs. full
- [ ] Sensitive-paths list (configurable per consuming repo, or hardcoded to start?)
- [ ] Which free-tier model to default to when no `LLM_API_KEY` is set
- [ ] Prompt section breakdown for v2 (security / quality / docs / ... — how many, which first)
- [ ] Comment format/template (structure of what gets posted to the PR)
