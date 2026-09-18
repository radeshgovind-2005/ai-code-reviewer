# AGENTS.md (example)

The AI reviewers read this file from your base branch. Keep it short and
factual: what the service does, conventions reviewers can't infer from a diff.

- This is a Flask API; all endpoints under `/admin` must call `require_admin()`.
- Money is stored as integer cents; floats for currency are a bug.
- Database access goes through `app/db.py`; raw SQL elsewhere needs a comment explaining why.
