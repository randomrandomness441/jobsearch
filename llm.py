"""LLM access: Z.ai first, then the Claude API, then the Claude CLI.

One public call, llm_json_call(). Two things the callers rely on:

    budget    set_budget(seconds) starts a wall-clock allowance for the current
              thread; every call after it is capped to what's left and gives up
              (returns None) once it's gone. budget_exceeded() says whether that
              is why a call failed, so the caller can record a timeout instead
              of a generic LLM error.
    usage     take_usage() returns and clears the token counts of the calls made
              on this thread since the last take -- what the metrics rows store.

Both are thread-local, so a request thread calling the LLM can't spend or
pollute the worker's budget.

No retries in here: a failed call returns None and the job-level retry (three
attempts, tracked in the analysis table) decides what happens next.
"""

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")
ZAI_MODEL = os.environ.get("ZAI_MODEL", "glm-5.3-flash")
# /coding/paas/v4/ is the endpoint the GLM Coding subscription covers; the
# general /api/paas/v4/ bills a separate pay-as-you-go wallet.
ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
DEFAULT_TEMPERATURE = float(os.environ.get("DEFAULT_TEMPERATURE", "0.3"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL_FALLBACK = os.environ.get("ANTHROPIC_MODEL_FALLBACK", "claude-haiku-4-5-20251001")
CLAUDE_CLI_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
_CLAUDE_CLI_DISALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,NotebookEdit,Task"

# Reasoning effort for each call site. "low" effort
# only: measured live, "high" took 239s against 23s for the same extraction
# and 1,251s without finishing on the scoring prompt, with no quality gain.
LLM_TUNING = {
    "score":          {"effort": "low"},
    "select":         {"effort": "low"},
    "draft":          {"effort": "low"},
    "gap_cluster":    {"effort": "low"},
}

_local = threading.local()
# Runs each HTTP request so a wall-clock deadline holds even when a socket
# trickles bytes (urllib's timeout= is per read, not per request).
_HTTP = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm-http")


# ── budget and usage (thread-local) ──────────────────────────────────────
def set_budget(seconds):
    _local.end = time.time() + seconds
    _local.exceeded = False
    _local.usage = []


def clear_budget():
    _local.end = None


def remaining():
    end = getattr(_local, "end", None)
    return None if end is None else end - time.time()


def budget_exceeded():
    return getattr(_local, "exceeded", False)


def last_provider():
    return getattr(_local, "provider", None)


def take_usage():
    """Summed token counts since the last take, and the list is cleared."""
    calls, _local.usage = getattr(_local, "usage", []), []
    total = {}
    for u in calls:
        for k, v in u.items():
            if v is not None:
                total[k] = total.get(k, 0) + v
    return total


def _record(usage):
    if not hasattr(_local, "usage"):
        _local.usage = []
    _local.usage.append(usage)


def _allowance(wanted):
    """Seconds this call may take: what it asked for, capped by the budget.
    None means the budget is spent and the call must not start."""
    left = remaining()
    if left is None:
        return wanted
    if left <= 1:
        _local.exceeded = True
        return None
    return min(wanted, left)


def _with_deadline(fn, seconds):
    try:
        return _HTTP.submit(fn).result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        if remaining() is not None and remaining() <= 1:
            _local.exceeded = True
        return None


# ── providers ────────────────────────────────────────────────────────────
def _zai_call(system_prompt, messages, max_tokens, temperature, model, effort, deadline_s):
    if not ZAI_API_KEY:
        print("[llm] ZAI_API_KEY not set", file=sys.stderr)
        return None
    seconds = _allowance(deadline_s)
    if seconds is None:
        return None
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "temperature": temperature,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "thinking": {"type": "enabled"}, "reasoning_effort": effort,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(ZAI_URL, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {ZAI_API_KEY}", "content-type": "application/json"})

    def _do():
        with urllib.request.urlopen(req, timeout=seconds + 10) as resp:
            return json.loads(resp.read())
    try:
        data = _with_deadline(_do, seconds)
    except urllib.error.HTTPError as e:
        print(f"[llm] Z.ai API error {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[llm] Z.ai call failed: {e}", file=sys.stderr)
        return None
    if data is None:
        print(f"[llm] Z.ai call exceeded its {seconds:.0f}s deadline", file=sys.stderr)
        return None
    u = data.get("usage", {})
    _record({"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
             "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
             "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens")})
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        print(f"[llm] Z.ai reply truncated at max_tokens={max_tokens}", file=sys.stderr)
    return choice["message"]["content"].strip()


def _anthropic_call(system_prompt, messages, max_tokens, temperature, model, deadline_s):
    if not ANTHROPIC_API_KEY:
        return None
    seconds = _allowance(deadline_s)
    if seconds is None:
        return None
    body = json.dumps({
        "model": model, "max_tokens": max_tokens, "temperature": temperature,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})

    def _do():
        with urllib.request.urlopen(req, timeout=seconds + 10) as resp:
            return json.loads(resp.read())
    try:
        data = _with_deadline(_do, seconds)
    except urllib.error.HTTPError as e:
        print(f"[llm] Anthropic API error {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[llm] Anthropic call failed: {e}", file=sys.stderr)
        return None
    if data is None:
        return None
    u = data.get("usage", {})
    _record({"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens"),
             "cached_tokens": u.get("cache_read_input_tokens")})
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def _flatten_messages(messages):
    """The CLI is one stateless `claude -p` call, so a multi-turn list (the
    style-gate retry loops append assistant/user turns) becomes one text block."""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return messages[0]["content"]
    return "\n\n".join(f"{'Assistant' if m['role'] == 'assistant' else 'User'}: {m['content']}" for m in messages)


def _claude_cli_call(system_prompt, messages, model, deadline_s):
    if not CLAUDE_CLI_OAUTH_TOKEN:
        return None
    seconds = _allowance(deadline_s)
    if seconds is None:
        return None
    cmd = ["claude", "-p", _flatten_messages(messages), "--output-format", "json",
           "--system-prompt", system_prompt, "--model", model, "--disable-slash-commands",
           "--strict-mcp-config", "--disallowedTools", _CLAUDE_CLI_DISALLOWED_TOOLS]
    try:
        out = subprocess.run(cmd, env=dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=CLAUDE_CLI_OAUTH_TOKEN),
                             capture_output=True, text=True, timeout=seconds)
    except Exception as e:
        print(f"[llm] Claude CLI failed to run: {e}", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[llm] Claude CLI exited {out.returncode}: {out.stderr[:300]}", file=sys.stderr)
        return None
    try:
        data = json.loads([l for l in out.stdout.splitlines() if l.strip()][-1])
        return None if data.get("is_error") else data.get("result")
    except Exception as e:
        print(f"[llm] Claude CLI reply wasn't parseable: {e}", file=sys.stderr)
        return None


# Zai is primary -- cheaper, and what every prompt was tuned against -- but its
# usage cap is real ("Usage limit reached for 5 hour" on a 429). After a Zai
# failure, skip straight to Claude for a few minutes instead of paying Zai's
# failure on every call.
_ZAI_COOLDOWN_SECONDS = 300
_zai_cooldown_until = 0.0


def llm_json_call(system_prompt, messages, tuning, max_tokens=32000, temperature=0.3, deadline_s=65):
    """(reply_text, provider) -- provider is "zai", "claude-api" or "claude-cli" --
    or (None, None) if every configured tier failed or the budget ran out.
    `tuning` is a key of LLM_TUNING. Every caller's prompt must already ask
    for ONLY a JSON object."""
    global _zai_cooldown_until
    cfg = LLM_TUNING[tuning]
    has_fallback = bool(ANTHROPIC_API_KEY or CLAUDE_CLI_OAUTH_TOKEN)
    if not has_fallback or time.time() >= _zai_cooldown_until:
        reply = _zai_call(system_prompt, messages, max_tokens, temperature, ZAI_MODEL, cfg["effort"], deadline_s)
        if reply is not None:
            _local.provider = "zai"
            return reply, "zai"
        if not has_fallback or budget_exceeded():
            return None, None
        _zai_cooldown_until = time.time() + _ZAI_COOLDOWN_SECONDS
        print(f"[llm] Zai failed -- falling back to Claude for {_ZAI_COOLDOWN_SECONDS}s", file=sys.stderr)
    reply = _anthropic_call(system_prompt, messages, max_tokens, temperature, ANTHROPIC_MODEL_FALLBACK, deadline_s)
    provider = "claude-api"
    if reply is None:
        reply = _claude_cli_call(system_prompt, messages, ANTHROPIC_MODEL_FALLBACK, deadline_s)
        provider = "claude-cli"
    if reply is None:
        return None, None
    _local.provider = provider
    return reply, provider
