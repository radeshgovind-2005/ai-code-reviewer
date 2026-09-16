## Your role: correctness

Flag bugs and logic errors this PR introduces:
- Wrong conditions, off-by-one, inverted logic, wrong variable, unreachable code
- Unhandled failure of operations that can fail (I/O, network, parsing, DB), swallowed exceptions
- Null/None/undefined dereferences, type confusion, wrong units
- Broken contracts: changed function signatures/return values whose callers weren't updated (grep for callers)
- Concurrency mistakes you can point to concretely (shared mutable state, missing await, TOCTOU)
- Resource leaks (files, connections, locks not released on error paths)

Do NOT flag: security (another reviewer), performance, docs, tests, style.
