# Playground: a deliberately bad PR

Seeded problems for trying the reviewer end to end. Files end in `.seed` so
this repository's own scanners and GitHub don't treat them as real; drop the
suffix when you copy them into a test repo (`scripts/make-playground-pr.sh`
does it for you).

| File | Planted problem | Caught by |
|---|---|---|
| `app/settings.py` | hardcoded API key + DB password | gitleaks, AI security |
| `app/handler.py` | `shell=True`, `pickle.loads`, `yaml.load` on request data | opengrep, AI security |
| `app/orders.py` | off-by-one, swallowed exception, N+1 query, no tests | AI correctness / performance / tests |
| `requirements.txt` | `requests==2.19.0`, `PyYAML==5.3` (known CVEs) | osv-scanner |
| `Dockerfile` | `:latest`, runs as root, `ADD` | trivy |
| `.github/workflows/pr.yml` | `pull_request_target` + checkout of PR head + title injection | zizmor, actionlint |
| `docs/api.md` | documents a parameter that no longer exists | AI docs |
