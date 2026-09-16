---
id: no-swallowed-exceptions
title: Don't swallow exceptions
level: MUST
status: enforced
paths: ["*.py", "*.ts", "*.js"]
---
Catching an exception must handle it, re-raise it, or log it with enough
context to debug. `except Exception: pass`, empty `catch {}` blocks, and
returning success after a caught failure are not allowed.
