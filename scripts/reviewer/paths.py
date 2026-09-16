"""Path pattern matching shared by agent path filters and standards.

Same semantics as sensitive_paths in tier-pr.sh:
  "auth/"        a directory with that name/path at any depth
  "Dockerfile"   a file (or directory) with that name/path at any depth
  "*.py", "**/x" anything containing * ? or [ is a glob on the full path;
                 * also matches "/", and a leading "**/" may match nothing
  "**"           everything
"""
from fnmatch import fnmatchcase


def matches(path: str, pattern: str) -> bool:
    while path.startswith("./"):
        path = path[2:]
    if pattern in ("**", "*"):
        return True
    if any(c in pattern for c in "*?["):
        if fnmatchcase(path, pattern):
            return True
        return pattern.startswith("**/") and fnmatchcase(path, pattern[3:])
    if pattern.endswith("/"):
        return path.startswith(pattern) or f"/{pattern}" in f"/{path}"
    return (
        path == pattern
        or path.endswith(f"/{pattern}")
        or path.startswith(f"{pattern}/")
        or f"/{pattern}/" in f"/{path}"
    )


def any_match(paths, patterns) -> bool:
    return any(matches(p, pat) for p in paths for pat in patterns)
