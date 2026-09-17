"""Runs one OpenCode call safely and reports what happened.

- Prompt goes on stdin (opencode `run` appends piped stdin to the message).
- `--format json` event stream is parsed for text, tokens, cost, finish
  reason and API errors.
- Hardened: project config/plugins/AGENTS.md/CLAUDE.md in the PR checkout are
  ignored (they could run code or inject instructions), a read-only agent is
  used, and the child gets a minimal environment (no GITHUB_TOKEN).
- Heartbeat log every `heartbeat` seconds; killed after `inactivity` seconds
  without output or at the hard timeout.
- Retries retryable errors (429/5xx/overloaded), falls back through a model
  chain on retryable or "model not found" errors, never on auth/context
  errors. A response cut off by max tokens is retried once.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
RETRYABLE_TEXT = re.compile(r"overloaded|rate.?limit|too many requests|timed? ?out|temporarily|unavailable|ECONNRESET", re.I)
NOT_FOUND_TEXT = re.compile(r"model.{0,40}not.?found|not_found_error|unknown model|does not exist|invalid model", re.I)
FATAL_TEXT = re.compile(r"invalid.?api.?key|authentication|unauthori[sz]ed|permission denied|context.{0,20}(length|window|overflow)|"
                        r"prompt is too long|maximum context", re.I)

HARDENING_ENV = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",   # no opencode.json / .opencode/ from the PR
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",      # no CLAUDE.md / .claude from the PR
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
}
BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                 "XDG_DATA_HOME", "XDG_STATE_HOME", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
                 "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
# Provider credentials OpenCode may need. Deliberately NOT GITHUB_TOKEN/GH_TOKEN.
PROVIDER_ENV_PATTERN = re.compile(r"^[A-Z0-9_]+_API_KEY$|^AWS_[A-Z_]+$|^AZURE_[A-Z_]+$|^GOOGLE_[A-Z_]+$|^OPENCODE_API_KEY$")
NEVER_PASS = {"GITHUB_TOKEN", "GH_TOKEN", "LLM_API_KEY", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"}


@dataclass
class Attempt:
    model: str
    status: str            # ok | error | timeout | inactive | truncated | empty
    duration: float
    error: str = ""
    http_status: int | None = None


@dataclass
class CallResult:
    ok: bool
    text: str = ""
    model: str = ""
    tokens: dict = field(default_factory=lambda: {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0})
    cost: float = 0.0
    duration: float = 0.0
    attempts: list = field(default_factory=list)
    error: str = ""


def child_env(extra_passthrough=()):
    env = {k: os.environ[k] for k in BASE_ENV_KEYS if k in os.environ}
    for k, v in os.environ.items():
        if k in NEVER_PASS:
            continue
        if PROVIDER_ENV_PATTERN.match(k) or any(k == p or (p.endswith("*") and k.startswith(p[:-1])) for p in extra_passthrough):
            env[k] = v
    env.update(HARDENING_ENV)
    return env


def parse_events(stdout_text):
    """Returns dict(text, tokens, cost, finish, error, http_status, retryable)."""
    out = {"texts": [], "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0},
           "cost": 0.0, "finish": None, "error": "", "http_status": None, "retryable": None}
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        typ = ev.get("type")
        part = ev.get("part") or {}
        if typ == "step_start":
            out["texts"].append([])
        elif typ == "text" and isinstance(part.get("text"), str):
            if not out["texts"]:
                out["texts"].append([])
            out["texts"][-1].append(part["text"])
        elif typ == "step_finish":
            tok = part.get("tokens") or {}
            cache = tok.get("cache") or {}
            for k_out, v in (("input", tok.get("input")), ("output", tok.get("output")), ("reasoning", tok.get("reasoning")),
                             ("cache_read", cache.get("read")), ("cache_write", cache.get("write"))):
                if isinstance(v, (int, float)):
                    out["tokens"][k_out] += int(v)
            if isinstance(part.get("cost"), (int, float)):
                out["cost"] += float(part["cost"])
            out["finish"] = part.get("reason") or out["finish"]
        elif typ == "error":
            err = ev.get("error") or {}
            data = err.get("data") or {}
            out["error"] = str(data.get("message") or err.get("name") or "unknown error")[:500]
            out["http_status"] = data.get("statusCode")
            out["retryable"] = data.get("isRetryable")
    # The answer is the text of the last step that produced text; earlier
    # steps are narration around tool calls.
    steps_with_text = [t for t in out["texts"] if t]
    out["text"] = "".join(steps_with_text[-1]) if steps_with_text else ""
    del out["texts"]
    return out


def classify(error, http_status, retryable):
    """-> 'retry' | 'fallback' | 'fatal'"""
    if http_status in (401, 403) or FATAL_TEXT.search(error or ""):
        return "fatal"
    if http_status == 404 or NOT_FOUND_TEXT.search(error or ""):
        return "fallback"
    if retryable or http_status in RETRYABLE_STATUS or RETRYABLE_TEXT.search(error or ""):
        return "retry"
    return "retry" if not http_status else "fatal"


def run_once(prompt, model, *, label, agent, config_path, cwd, timeout, inactivity, heartbeat,
             message, passthrough=(), log=sys.stderr):
    cmd = ["opencode", "run", "--format", "json", "--agent", agent, "-m", model, message]
    env = child_env(passthrough)
    if config_path:
        env["OPENCODE_CONFIG"] = config_path
    start = time.monotonic()
    last_output = [start]
    chunks = []
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError:
        return None, Attempt(model, "error", 0.0, "opencode not found on PATH")

    def feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def read_out():
        for line in proc.stdout:
            chunks.append(line)
            last_output[0] = time.monotonic()

    err_chunks = []

    def read_err():
        for line in proc.stderr:
            err_chunks.append(line)

    threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, read_err)]
    for t in threads:
        t.start()

    status = None
    next_beat = start + heartbeat
    while proc.poll() is None:
        now = time.monotonic()
        if now - start > timeout:
            status = "timeout"
        elif now - last_output[0] > inactivity:
            status = "inactive"
        if status:
            proc.kill()
            break
        if heartbeat and now >= next_beat:
            print(f"[{label}] {model}: still thinking ({int(now - last_output[0])}s since last output, "
                  f"{int(now - start)}s total)", file=log, flush=True)
            next_beat = now + heartbeat
        time.sleep(0.2)
    proc.wait()
    for t in threads[1:]:
        t.join(timeout=5)
    duration = time.monotonic() - start
    if status == "timeout":
        return None, Attempt(model, "timeout", duration, f"timed out after {timeout}s")
    if status == "inactive":
        return None, Attempt(model, "inactive", duration, f"no output for {inactivity}s")

    ev = parse_events("".join(chunks))
    if ev["error"]:
        return ev, Attempt(model, "error", duration, ev["error"], ev["http_status"])
    if proc.returncode != 0:
        tail = "".join(err_chunks)[-300:].strip()
        return ev, Attempt(model, "error", duration, f"opencode exited {proc.returncode}: {tail}")
    if ev["finish"] == "length":
        return ev, Attempt(model, "truncated", duration, "response hit the max output tokens")
    if not ev["text"].strip():
        return ev, Attempt(model, "empty", duration, "empty response")
    return ev, Attempt(model, "ok", duration)


def call(prompt, models, *, label, agent="ai-reviewer", config_path=None, cwd=None, timeout=600,
         inactivity=300, heartbeat=30, retries=1, backoff=None, deadline=None, min_retry_budget=60,
         message="Review the pull request described below, following all instructions in it.",
         passthrough=(), log=sys.stderr) -> CallResult:
    """Tries each model in order. Returns the first good response."""
    backoff = float(os.environ.get("REVIEW_BACKOFF_SECONDS", "5")) if backoff is None else backoff
    result = CallResult(ok=False)
    started = time.monotonic()
    for model in models:
        tries = 0
        truncated_retry_used = False
        current_prompt = prompt
        while True:
            remaining = (deadline - time.monotonic()) if deadline else timeout
            if remaining <= 0:
                result.error = "overall review deadline reached"
                result.duration = time.monotonic() - started
                return result
            ev, att = run_once(current_prompt, model, label=label, agent=agent, config_path=config_path, cwd=cwd,
                               timeout=min(timeout, remaining), inactivity=inactivity, heartbeat=heartbeat,
                               message=message, passthrough=passthrough, log=log)
            result.attempts.append(att)
            if ev:
                for k, v in ev["tokens"].items():
                    result.tokens[k] += v
                result.cost += ev["cost"]
            if att.status == "ok":
                result.ok, result.text, result.model = True, ev["text"], model
                result.duration = time.monotonic() - started
                return result
            print(f"[{label}] {model}: {att.status}: {att.error}", file=log, flush=True)
            result.error = f"{model}: {att.status}: {att.error}"
            budget_ok = not deadline or (deadline - time.monotonic()) > min_retry_budget
            if att.status == "truncated" and not truncated_retry_used and budget_ok:
                truncated_retry_used = True
                current_prompt = prompt + ("\n\nIMPORTANT: your previous answer was cut off by the output limit. "
                                           "Be much more concise: at most 10 findings, one or two sentences each.")
                continue
            if att.status in ("timeout", "inactive", "truncated", "empty"):
                break  # try the next model
            kind = classify(att.error, att.http_status, None)
            if kind == "fatal":
                result.duration = time.monotonic() - started
                return result
            if kind == "retry" and tries < retries and budget_ok:
                tries += 1
                time.sleep(backoff * tries)
                continue
            break  # fallback to the next model
    result.duration = time.monotonic() - started
    return result
