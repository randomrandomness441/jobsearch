#!/usr/bin/env python3
"""jobsearch resume-automation server. Single user, no dependencies outside
the stdlib for this process (DOCX rendering shells into this project's own
.venv, see VENV_PY below).

    ./server.py        then open http://localhost:8777

Split out of the former combined cpp/platform/server.py, which also served
an unrelated C++ prep platform from the same process -- this file owns only
the jobsearch routes/LLM calls, as its own standalone HTTP server.
"""

import base64
import collections
import concurrent.futures
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as jobsdb
import ats as jobs_ats
import humanizer as jobs_humanizer

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
APPLICATIONS = os.path.join(ROOT, "data", "applications")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python3")
PORT = int(os.environ.get("PORT", "8777"))
# 127.0.0.1 by default (local dev only, matches old behavior) -- a VPS deploy needs
# HOST=0.0.0.0 in its .env, or the server only ever accepts connections from inside
# the VPS itself, which looks exactly like "connection refused" from outside.
HOST = os.environ.get("HOST", "127.0.0.1")

# Shared by every truthful-writing prompt below -- what actually makes text
# read as AI-generated (em dashes, "not X but Y", hedging) rather than a
# person talking.
HUMAN_STYLE_RULES = """
How you talk (write like a person speaking, not like an AI assistant):
- One idea per sentence. If you catch yourself adding a parenthetical or a clause after
  a comma to sneak in a second idea, stop and make it its own sentence instead.
- Plain connecting words only: and, but, so, because. Never "furthermore", "moreover",
  "additionally".
- Never use an em dash. Use a period or a comma.
- Never write "it's not just X, it's Y".
- Never use "delve", "leverage", "underscore", "boast", "robust", "seamless".
- Say things directly. Don't hedge with "can be complex" or "it's worth noting that".
- Don't close with "let me know if you have any questions" or similar.
"""


def _jobs_resume_body():
    """(profile, skill_names, bullets_text, skills_line_text, candidate_years)
    -- the untailored candidate baseline, used to build the LLM-judge
    prompt (the only place a real fit judgment happens -- regex only ever
    does title/seniority/location/date classification, never scoring).
    bullets_text and skills_line_text are kept separate so the judge prompt
    can tell a skill demonstrated in real point-bank prose apart from one
    only present in the bare Skills line."""
    profile = jobsdb.profile_all()
    skill_names = [s["name"] for s in jobsdb.skills_all()]
    points = [p for p in jobsdb.point_bank_all() if not p["text"].lstrip().startswith("[")]
    bullets_text = "\n".join(p["text"] for p in points)
    skills_line_text = ", ".join(skill_names)
    candidate_years = float(profile.get("years_experience", 0) or 0)
    return profile, skill_names, bullets_text, skills_line_text, candidate_years


def _parse_json_col(value):
    """jobs.llm_missing/llm_projects are stored as JSON text -- this is the
    one place that parses either, so a malformed/empty value degrades to
    an empty list instead of crashing or leaking a raw JSON string to a
    caller that expects a real array."""
    try:
        return json.loads(value or "[]")
    except Exception:
        return []


def _job_public(j, applied_ids):
    # "stale" (judged, but your master doc changed since) is deliberately
    # distinct from "never judged" -- jobs_llm_score_stale() returns True
    # for both, but the UI already has its own "not judged" treatment for
    # the never-judged case, so only surface the badge for the case that's
    # actually new information: a score you're looking at right now that
    # no longer reflects your current profile. There's no separate stored
    # flag to go out of sync -- this is computed fresh from llm_judged_ts
    # vs profile_version_ts() on every read, and llm_judged_ts only moves
    # when THAT posting is individually re-judged, so it can't clear any
    # other way.
    stale = j["llm_score"] is not None and jobsdb.jobs_llm_score_stale(j)
    return {
        "id": j["id"], "company": j["company"], "title": j["title"], "url": j["url"],
        "source": j["source"], "status": j["status"],
        "fetched_ts": j["fetched_ts"],
        "is_engineering": jobs_ats.is_likely_engineering_title(j["title"]),
        "seniority_tier": jobs_ats.title_seniority_tier(j["title"]),
        "llm_score": j["llm_score"], "llm_reason": j["llm_reason"],
        "llm_missing": _parse_json_col(j["llm_missing"]),
        "llm_projects": _parse_json_col(j["llm_projects"]), "stale": stale,
        "location": j["location"],
        "applied": j["id"] in applied_ids,
    }


def _day_start_ts(days_ago):
    """Midnight, local time, N days ago -- for the "fetched within the
    last N days" filter, so a posting that's been open for weeks doesn't
    keep resurfacing as if it just showed up."""
    t = time.localtime()
    midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight - days_ago * 86400


def _days_cutoff(days_str, default=None):
    """Parses a plain day-count string (the "fetched within ___ days"
    input) into a fetched_ts cutoff -- blank/0/not-a-number means no
    cutoff at all (any time fetched), same as before this was a dropdown.
    default: the day count to use when days_str is missing entirely
    (the key wasn't sent), not when it's present but blank -- those are
    different things (no opinion vs. an explicit "any time")."""
    if days_str is None:
        days_str = default
    try:
        days = int(days_str)
    except (TypeError, ValueError):
        return None
    return _day_start_ts(days) if days > 0 else None


def _filter_jobs(qs):
    """Shared by /api/jobs/list and /api/jobs/companies so the two always
    agree on what "matching" means -- a company's card count/best-score and
    the list you get after clicking into it must be the same filter,
    computed once, not two versions that can drift apart.

    This is a thin param-parsing layer over jobsdb.jobs_query -- the actual
    filtering is a SQL WHERE clause now, not a Python loop over every row
    (what this used to be). Returns (total_unfiltered, matched_jobs),
    matched_jobs already sorted newest-fetched first (jobs_query's own
    order) -- the company-card best_score below is computed by an explicit
    max-comparison loop, not by assuming row order, so this sort change
    doesn't affect it."""
    min_score = qs.get("min_score", [None])[0]
    eng_only = qs.get("eng_only", ["1"])[0] == "1"
    q_text = (qs.get("q", [""])[0] or "").lower()
    # level: "core" (mid+senior -- the realistic target band) is the
    # default; "stretch" also allows staff/senior_staff; "all" adds
    # manager. Not a score penalty like years-of-experience -- this is a
    # different tier of role entirely, not a worse fit for the same tier,
    # so it's a filter, not a point deduction.
    level = qs.get("level", ["core"])[0]
    level_tiers = {
        "core": {"mid", "senior"},
        "stretch": {"mid", "senior", "staff", "senior_staff"},
        "all": {"mid", "senior", "staff", "senior_staff", "manager"},
    }.get(level, {"mid", "senior"})
    company_filter = (qs.get("company", [""])[0] or "").lower()
    source_filter = qs.get("source", [""])[0]   # "" (any) | "manual" | "auto"
    date_cutoff = _days_cutoff(qs.get("days", [None])[0])

    return jobsdb.jobs_query(
        status=qs.get("status", [None])[0], eng_only=eng_only, level_tiers=level_tiers,
        min_score=min_score, q_text=q_text, company_filter=company_filter,
        source_filter=source_filter, date_cutoff=date_cutoff, exclude_applied=True)


# ── LLM provider config ──────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Fallback JSON-task tier (judge/draft/extract via _llm_json_call), both the
# real-API and CLI branches -- Haiku 4.5, per Anthropic's own documented
# guidance ("Choose Haiku for simple tasks, Sonnet for most production
# workloads" -- platform.claude.com/docs/en/about-claude/pricing). Half the
# price of Sonnet on both input and output tokens. Mirrors ZAI_MODEL/
# ZAI_MODEL_FULL below for the same reason: these are high-volume, single-
# shot JSON judgments, not an open-ended conversation.
ANTHROPIC_MODEL_FALLBACK = os.environ.get("ANTHROPIC_MODEL_FALLBACK", "claude-haiku-4-5-20251001")
# Reserved for the one call site writing into the master document itself
# (_zai_rewrite_point) -- same reasoning as ZAI_MODEL_FULL.
ANTHROPIC_MODEL_FULL = os.environ.get("ANTHROPIC_MODEL_FULL", "claude-sonnet-5")

ZAI_API_KEY = os.environ.get("ZAI_API_KEY", "")

# Signs the one-click "Generate now" link the poll script emails out --
# the signature itself IS the auth for that one link (clicked straight
# from an email, not a live logged-in session), so it must never be
# guessable. Falls back to the Zai key so a from-scratch deploy that
# hasn't set this separately still gets a real per-install secret rather
# than a constant every clone of this repo would share -- set
# GENERATE_LINK_SECRET explicitly once this is actually deployed.
GENERATE_LINK_SECRET = os.environ.get("GENERATE_LINK_SECRET", "") or ZAI_API_KEY


def _sign_job_id(jid):
    return hmac.new(GENERATE_LINK_SECRET.encode(), str(jid).encode(), hashlib.sha256).hexdigest()


def _verify_job_sig(jid, sig):
    if not GENERATE_LINK_SECRET:
        return False
    return hmac.compare_digest(_sign_job_id(jid), sig or "")


# ── Google OAuth login gate ──────────────────────────────────────────────
# Single-user: the only thing checked after Google confirms identity is
# "does this email match ALLOWED_LOGIN_EMAIL" -- not a real multi-user
# system, a lock on the front door for one person.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
ALLOWED_LOGIN_EMAIL = os.environ.get("ALLOWED_LOGIN_EMAIL", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", f"http://localhost:{PORT}/auth/google/callback")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "") or GENERATE_LINK_SECRET
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days -- one person's own tool, not a shared login
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Reachable with no session at all: the login flow itself (can't require a
# session to reach the page that creates one), and the generate-link/
# view-details endpoints, which already have their own HMAC-signature auth
# (_verify_job_sig) -- that's the whole point of them being one-click links
# from a Telegram notification, not something that requires first being
# logged in.
AUTH_EXEMPT_PREFIXES = ("/auth/google/", "/api/jobs/generate-link", "/api/jobs/view-details")


def _make_session_cookie(email):
    expiry = int(time.time()) + SESSION_MAX_AGE
    payload = f"{email}|{expiry}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def _parse_cookies(cookie_header):
    cookies = {}
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies


def _valid_session_email(cookie_header):
    """The logged-in email if the session cookie is present, correctly
    signed, unexpired, and matches ALLOWED_LOGIN_EMAIL -- None otherwise.
    A forged, tampered, or expired cookie is treated identically to no
    cookie at all, never partially trusted."""
    raw = _parse_cookies(cookie_header).get("session")
    if not raw:
        return None
    try:
        email, expiry, sig = raw.split("|", 2)
    except ValueError:
        return None
    expected = hmac.new(SESSION_SECRET.encode(), f"{email}|{expiry}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if int(expiry) < time.time():
        return None
    if email != ALLOWED_LOGIN_EMAIL:
        return None
    return email


def _google_auth_redirect_url(state):
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code", "scope": "openid email profile",
        "state": state, "prompt": "select_account",
    }
    return GOOGLE_AUTH_URL + "?" + urlencode(params)


def _google_exchange_code(code):
    """POSTs the auth code to Google's token endpoint server-to-server
    (the one place GOOGLE_OAUTH_CLIENT_SECRET is ever used), then calls
    the userinfo endpoint with the resulting access token. Returns the
    logged-in email, or None on any failure -- treated as "login failed",
    never as a free pass."""
    data = urlencode({
        "code": code, "client_id": GOOGLE_OAUTH_CLIENT_ID, "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT_URI, "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST",
                                  headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read())
    except Exception as e:
        print(f"[auth] token exchange failed: {e}", file=sys.stderr)
        return None
    access_token = tok.get("access_token")
    if not access_token:
        print(f"[auth] token exchange returned no access_token: {tok}", file=sys.stderr)
        return None
    req2 = urllib.request.Request(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req2, timeout=15) as resp:
            info = json.loads(resp.read())
    except Exception as e:
        print(f"[auth] userinfo fetch failed: {e}", file=sys.stderr)
        return None
    return info.get("email")


ZAI_MODEL = os.environ.get("ZAI_MODEL", "glm-5.3-flash")
# Full (non-Flash) GLM-5.3 -- ~13-20x the cost of Flash per Z.ai's own
# pricing (docs.z.ai, checked live). Which call sites use which tier, and
# at what reasoning_effort, is a config decision, not scattered per-call
# kwargs -- see LLM_TUNING below, the one place that answers "what does X
# actually run on".
ZAI_MODEL_FULL = os.environ.get("ZAI_MODEL_FULL", "glm-5.3")
# /coding/paas/v4/ specifically -- that's the endpoint the GLM Coding Lite
# subscription actually covers. The general /api/paas/v4/ (no "coding")
# bills from a separate pay-as-you-go wallet and 429s "Insufficient
# balance" the moment that's empty, even with an active coding-plan key --
# same models, different billing rail. Confirmed by the exact error hit.
ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
# Default temperature when a caller doesn't pass one explicitly -- every
# real jobsearch call site below does pass one, so this is a safety floor,
# not something actually relied on.
DEFAULT_TEMPERATURE = float(os.environ.get("DEFAULT_TEMPERATURE", "0.3"))
# Z.ai doesn't publish a fixed concurrency number -- it's dynamic per
# account/plan tier (see z.ai/manage-apikey/rate-limits for the real one).
# PER MODEL, not account-wide -- confirmed two ways: third-party research
# ("hitting the ceiling on glm-5.3 does not stop you calling glm-5.3-flash")
# and a real live test (2x concurrent glm-5.3 calls, 2x concurrent
# glm-5.3-flash calls, and 1x of each together -- all three combinations
# succeeded with zero 429s). Flash specifically gets 2, not 1 -- measured
# live with the actual heavy generate-and-score prompt shape (~50-65s
# each), fired concurrently: both completed cleanly, no 429s.
_ZAI_CONCURRENCY = collections.defaultdict(lambda: threading.Semaphore(1))
_ZAI_CONCURRENCY[ZAI_MODEL] = threading.Semaphore(2)
# The live test above already measured glm-5.3 (full) itself at 2x concurrent
# with zero 429s -- same result as flash -- but only flash ever got the
# Semaphore(2) below it; glm-5.3 fell through to the conservative default of
# 1 and was serializing every full-tier call for no measured reason.
_ZAI_CONCURRENCY[ZAI_MODEL_FULL] = threading.Semaphore(2)

# ── LLM tuning: one table, every real call site looks itself up here ────
# Two independent knobs per call site:
#   model  -- "flash" (glm-5.3-flash, cheap/fast) or "full" (glm-5.3,
#             ~13-20x the cost) -- and the matching Claude-side tier when
#             Zai is down (ANTHROPIC_MODEL_FALLBACK/_FULL).
#   effort -- Z.ai's reasoning_effort: "low" (quick) or "high" (slower,
#             more careful). Z.ai defaults this to "max" if ever omitted
#             entirely (docs.z.ai/guides/llm/glm-5.3) -- _zai_call always
#             sends one explicitly so that silent default never applies.
# Change behavior by editing a row here, not by hunting down a call site.
LLM_TUNING = {
    # The two places that actually write into the master document --
    # reshaping a bullet, or proposing new ones from pasted/uploaded text.
    # Full model, but LOW effort, not high -- high was tried on
    # extract_profile with a real pasted resume and measured the same
    # problem as rescore_single/generate_single: 239.5s at high effort vs
    # 23.4s at low, same inputs, same quality (same points extracted, same
    # needs_review flags on weak bullets) -- high just isn't practically
    # usable at this prompt size on this model. rewrite_point shares the
    # identical risk and was reverted preemptively rather than waiting to
    # hit the same wall on a real master-doc edit.
    "rewrite_point":   {"model": "full",  "effort": "low"},
    "extract_profile": {"model": "full",  "effort": "low"},
    # AI-judge: bulk triage over many postings at once. Flash, low effort --
    # cheap and fast on purpose, this is the volume path.
    "judge_batch":     {"model": "flash", "effort": "low"},
    # One posting, re-scored on demand (e.g. right after a master-doc
    # edit). "high" effort was tried here and measured broken, not just
    # slow: with max_tokens raised to 32000 and the deadline raised to
    # 240s, one real call to this exact prompt shape took 1251s (20.9 min)
    # across 3 attempts and STILL never completed (ended in a connection
    # reset). "low" effort on the same prompt: 52s, complete, correct.
    "rescore_single":  {"model": "flash", "effort": "low"},
    # Resume + cover letter for ONE specific job posting (the "add project
    # and regenerate" single-job action) -- the actual content that ships.
    "generate_single": {"model": "flash", "effort": "low"},
    # The generate-queued batch loop -- same "batch = low effort" treatment
    # as judge_batch, even though each iteration is one job at a time.
    "generate_batch":  {"model": "flash", "effort": "low"},
    # Gap-cluster analysis (Gaps tab).
    "gap_cluster":     {"model": "flash", "effort": "low"},
}


def _llm_tuning(key):
    """Resolve an LLM_TUNING key into the kwargs _llm_json_call/_zai_call
    actually take. The one function every real call site routes through --
    see LLM_TUNING's own comment for what each key means."""
    cfg = LLM_TUNING[key]
    full = cfg["model"] == "full"
    return {
        "zai_model": ZAI_MODEL_FULL if full else ZAI_MODEL,
        "claude_model": ANTHROPIC_MODEL_FULL if full else ANTHROPIC_MODEL_FALLBACK,
        "reasoning_effort": cfg["effort"],
        # "high" effort measured live taking 110s+ even BEFORE the
        # max_tokens raise above (which gives it room to run longer, not
        # shorter) -- the old hardcoded 65s deadline killed and retried
        # every single high-effort call 3x before ever letting one finish.
        # 240s gives real margin; "low" effort keeps the original 65s --
        # it measured 52s end-to-end, no reason to wait longer on a stuck
        # connection for calls that normally finish fast.
        "deadline_s": 240 if cfg["effort"] == "high" else 65,
    }


# A plain urllib `timeout=` is a PER-SOCKET-OPERATION timeout, not a real
# wall-clock deadline -- a connection that trickles bytes slowly enough can
# reset that clock on every read and never time out at all. Running the
# request in a worker and bounding it with Future.result(timeout=...)
# enforces a genuine deadline regardless of what the socket is doing.
_HTTP_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="llm-http")


def _with_deadline(fn, deadline_s):
    try:
        return _HTTP_EXECUTOR.submit(fn).result(timeout=deadline_s)
    except concurrent.futures.TimeoutError:
        return "__DEADLINE_EXCEEDED__"


def _anthropic_call(system_prompt, messages, max_tokens=1200, temperature=None, model=None):
    if not ANTHROPIC_API_KEY:
        print("[jobs] ANTHROPIC_API_KEY not set -- skipping automatic reply", file=sys.stderr)
        return None
    body = json.dumps({
        "model": model or ANTHROPIC_MODEL_FALLBACK,
        "max_tokens": max_tokens,
        "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        def _do():
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        data = _with_deadline(_do, 65)
        if data == "__DEADLINE_EXCEEDED__":
            print("[jobs] Anthropic call exceeded 65s wall-clock deadline, giving up", file=sys.stderr)
            return None
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    except urllib.error.HTTPError as e:
        print(f"[jobs] Anthropic API error {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
    except Exception as e:
        print(f"[jobs] Anthropic API call failed: {e}", file=sys.stderr)
    return None


# Last-resort Claude access with no billed API key: shells out to the
# `claude` CLI itself, authenticated with a long-lived OAuth token from
# `claude setup-token` (CLAUDE_CODE_OAUTH_TOKEN) instead of ANTHROPIC_API_KEY
# -- draws on the Claude subscription's own usage window, not a separate
# dollar-metered account. Same officially-documented mechanism Anthropic's
# own claude-code-action GitHub Action uses for CI.
CLAUDE_CLI_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
CLAUDE_CLI_TIMEOUT = 90
# Every tool a coding session would need is irrelevant to (and only adds
# risk/latency to) a plain "answer this JSON question" call -- denying them
# explicitly cuts real token overhead versus the harness defaults.
_CLAUDE_CLI_DISALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,NotebookEdit,Task"


def _flatten_messages(messages):
    """The CLI fallback is one stateless `claude -p <prompt>` call, no
    --continue -- so a multi-turn messages list (the humanizer-retry loop
    in _zai_draft_application appends assistant/user turns) has to become
    one plain-text block instead of a real conversation."""
    if len(messages) == 1 and messages[0]["role"] == "user":
        return messages[0]["content"]
    return "\n\n".join(f"{'Assistant' if m['role'] == 'assistant' else 'User'}: {m['content']}"
                        for m in messages)


def _claude_cli_call(system_prompt, messages, max_tokens=1200, temperature=None, model=None):
    if not CLAUDE_CLI_OAUTH_TOKEN:
        return None
    cmd = ["claude", "-p", _flatten_messages(messages), "--output-format", "json",
           "--system-prompt", system_prompt, "--model", model or ANTHROPIC_MODEL_FALLBACK,
           "--disable-slash-commands", "--strict-mcp-config",
           "--disallowedTools", _CLAUDE_CLI_DISALLOWED_TOOLS]
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=CLAUDE_CLI_OAUTH_TOKEN)
    try:
        out = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=CLAUDE_CLI_TIMEOUT)
    except Exception as e:
        print(f"[llm] Claude CLI fallback failed to run: {e}", file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f"[llm] Claude CLI fallback exited {out.returncode}: {out.stderr[:300]}", file=sys.stderr)
        return None
    try:
        lines = [l for l in out.stdout.splitlines() if l.strip()]
        data = json.loads(lines[-1])
        if data.get("is_error"):
            print(f"[llm] Claude CLI fallback returned an error result: {str(data)[:300]}", file=sys.stderr)
            return None
        return data.get("result")
    except Exception as e:
        print(f"[llm] Claude CLI fallback reply wasn't parseable JSON: {e} -- raw: {out.stdout[:300]}",
              file=sys.stderr)
        return None


def _zai_call(system_prompt, messages, max_tokens=1200, temperature=None,
              response_format=None, model=None, reasoning_effort="low", deadline_s=65,
              thinking=True):
    # thinking=False is a documented no-op for glm-5.3/glm-5.3-flash (the only two models
    # this file ever calls) -- Z.ai's own API reference: "GLM-5.3 GLM-5.3-FLASH series can
    # only be enabled, and the thinking depth is controlled by reasoning_effort" (confirmed
    # on both the model guide pages and the chat-completion schema, 2026-10-16). A real
    # pilot run that sent thinking=False got full, successful completions back with no
    # error either time -- the request almost certainly ran with thinking still on and the
    # field was silently ignored, not actually disabled; don't trust that result as a real
    # A/B test. The only real lever for reasoning depth/latency on these models is
    # reasoning_effort (low/high/max) -- LLM_TUNING already runs every generate/judge call
    # site at "low", the fastest tier these models support. Kept as a real param, not
    # removed, since a future call site on an older model (glm-4.x) could genuinely use it.
    if not ZAI_API_KEY:
        print("[jobs] ZAI_API_KEY not set -- skipping automatic reply", file=sys.stderr)
        return None
    body_obj = {
        "model": model or ZAI_MODEL,
        "max_tokens": max_tokens,
        "temperature": DEFAULT_TEMPERATURE if temperature is None else temperature,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "reasoning_effort": reasoning_effort,
    }
    if response_format:
        body_obj["response_format"] = {"type": response_format}
    body = json.dumps(body_obj).encode()
    req = urllib.request.Request(
        ZAI_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {ZAI_API_KEY}", "content-type": "application/json"})
    with _ZAI_CONCURRENCY[body_obj["model"]]:
        delay = 1.0
        for attempt in range(3):
            try:
                def _do():
                    with urllib.request.urlopen(req, timeout=deadline_s + 10) as resp:
                        return json.loads(resp.read())
                data = _with_deadline(_do, deadline_s)
                if data == "__DEADLINE_EXCEEDED__":
                    if attempt < 2:
                        # Giving up here does NOT kill the abandoned request -- it's
                        # still running in _HTTP_EXECUTOR, possibly about to succeed,
                        # and retrying instantly just fires a second real (billed)
                        # request on top of it, competing for the same per-model
                        # concurrency slot it's already holding. A real backoff (same
                        # policy as the 429 branch below) gives the orphaned attempt a
                        # head start to actually finish or die on its own inner timeout
                        # (deadline_s + 10) before a fresh one piles on -- found live:
                        # a single job retried 3x back-to-back with zero gap took
                        # 2-3 minutes for what should have been one ~65s call.
                        print(f"[jobs] Z.ai call exceeded {deadline_s}s wall-clock deadline, retrying "
                              f"in {delay:.0f}s (attempt {attempt + 2}/3)", file=sys.stderr)
                        time.sleep(delay)
                        delay *= 2
                        continue
                    print(f"[jobs] Z.ai call exceeded {deadline_s}s wall-clock deadline on the last "
                          "attempt, giving up", file=sys.stderr)
                    return None
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    usage = data.get("usage", {})
                    print(f"[jobs] Z.ai reply TRUNCATED at max_tokens={max_tokens} "
                          f"(reasoning_tokens={usage.get('completion_tokens_details', {}).get('reasoning_tokens')}, "
                          f"completion_tokens={usage.get('completion_tokens')})", file=sys.stderr)
                return choice["message"]["content"].strip()
            except urllib.error.HTTPError as e:
                body_text = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 2:
                    print(f"[jobs] Z.ai 429, retrying in {delay:.0f}s: {body_text[:200]}", file=sys.stderr)
                    time.sleep(delay)
                    delay *= 2
                    continue
                print(f"[jobs] Z.ai API error {e.code}: {body_text}", file=sys.stderr)
                return None
            except Exception as e:
                if attempt < 2:
                    print(f"[jobs] Z.ai call failed ({e}), retrying in {delay:.0f}s", file=sys.stderr)
                    time.sleep(delay)
                    delay *= 2
                    continue
                print(f"[jobs] Z.ai API call failed on the last attempt: {e}", file=sys.stderr)
                return None
        return None


# Fallback mode for every structured JSON call below (judge/draft/rewrite/
# extract): Zai is primary -- cheaper and it's what every one of those
# prompts was tuned against -- but Zai's own usage cap is real (hit live:
# "Usage limit reached for 5 hour" on a real 429). Without a fallback that
# just stops a judge/draft batch mid-run for hours. _llm_json_call is the
# ONE place every one of those call sites routes through: Zai first, Claude
# on any Zai failure.
#
# A short cooldown after a Zai failure skips straight to Claude for a while
# instead of re-paying Zai's own ~3s retry/backoff on every single call in
# a batch of dozens -- then tries Zai again once it lapses.
_ZAI_COOLDOWN_SECONDS = 300
_zai_cooldown_until = 0.0


def _llm_json_call(system_prompt, messages, max_tokens=1200, temperature=0.3, reasoning_effort="low",
                    zai_model=None, claude_model=None, deadline_s=65):
    """One JSON-shaped LLM call, routed to whichever provider is actually
    up: Zai (Flash by default, cheap/fast, primary) -> a real Anthropic API
    key if one's ever configured (ANTHROPIC_API_KEY) -> the Claude CLI on
    an OAuth subscription token (CLAUDE_CODE_OAUTH_TOKEN) as the true no-
    credits-required last resort. Returns (reply_text, provider) where
    provider is "zai", "claude-api", or "claude-cli", or (None, None) if
    every configured tier failed (or none are configured at all).

    zai_model overrides ZAI_MODEL (the Flash default); claude_model
    overrides ANTHROPIC_MODEL_FALLBACK (the Haiku default) on BOTH Claude
    branches -- each used by the one call site that wants the full,
    highest-stakes model instead of the cheap default for every provider
    at once.

    Every caller's system_prompt must already say "reply with ONLY a JSON
    object" in its own words, and every caller already pulls the {...} out
    with a tolerant regex before json.loads."""
    global _zai_cooldown_until
    has_real_fallback = bool(ANTHROPIC_API_KEY or CLAUDE_CLI_OAUTH_TOKEN)
    if not has_real_fallback or time.time() >= _zai_cooldown_until:
        reply = _zai_call(system_prompt, messages, max_tokens=max_tokens, temperature=temperature,
                           reasoning_effort=reasoning_effort, response_format="json_object", model=zai_model,
                           deadline_s=deadline_s)
        if reply is not None:
            return reply, "zai"
        if not has_real_fallback:
            return None, None
        _zai_cooldown_until = time.time() + _ZAI_COOLDOWN_SECONDS
        print(f"[llm] Zai call failed -- falling back to Claude, will retry Zai again in "
              f"{_ZAI_COOLDOWN_SECONDS}s", file=sys.stderr)
    reply = _anthropic_call(system_prompt, messages, max_tokens=max_tokens, temperature=temperature,
                             model=claude_model or ANTHROPIC_MODEL_FALLBACK)
    if reply is not None:
        return reply, "claude-api"
    reply = _claude_cli_call(system_prompt, messages, max_tokens=max_tokens, temperature=temperature,
                              model=claude_model or ANTHROPIC_MODEL_FALLBACK)
    return (reply, "claude-cli") if reply is not None else (None, None)


# Primary job-fit judgment. Gated by title + location alone
# (is_likely_engineering_title, non-manager tier, India/Bangalore/Bengaluru
# -- the same classification the Postings list itself filters on) -- NOT
# by a regex score. Also hard-capped per fetch so a big backlog can't run
# away on cost.
JOBS_LLM_MAX_PER_FETCH = 25         # hard cap on calls per Fetch & score run
JOBS_JD_CHAR_LIMIT = 12000          # covers all but the single longest fetched posting in full


def _trim_jd(text, limit=JOBS_JD_CHAR_LIMIT):
    """Truncate at a real word/line boundary, not mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > limit * 0.9:   # don't backtrack far enough to lose a lot
        cut = cut[:boundary]
    return cut + "\n\n[...truncated]"


def _zai_score_requirements(job, profile, point_bank_items, candidate_years, tuning_key="judge_batch"):
    """Call 1 of the resume judge/selector split: scores this one JD against
    the candidate's real role + project items -- requirements (the sole
    source of truth for `score`), project worth_including/reason, and gaps.
    Never picks resume content or words anything -- that's
    _zai_select_content's job (Call 2), which takes this call's
    `requirements` as grounding so the two calls agree on what actually
    matters without one re-deriving the other's work. Used directly by
    _score_only (AI-judge batch, single-job rescore) -- those callers never
    need content selection, and paying for it on every stale/new posting
    was pure waste (judge_batch runs over far more postings than real
    generation ever does).

    tuning_key: which LLM_TUNING row this call runs under -- "judge_batch"/
    "rescore_single" for score-only callers, "generate_batch"/
    "generate_single" when called as half of _zai_generate_and_score below.

    point_bank_items: jobsdb.point_bank_all() rows (role and project items
    together, each with its real id/company/role/dates/entry_type/text).

    Same structured-rubric discipline the old combined call used
    (importance/presence/depth per requirement, real JSON not a vibe).

    Returns {"requirements", "missing", "project_assessment", "score",
    "reason"}, or None if the call/parse failed."""
    role_items_text = "\n".join(
        f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
        for p in point_bank_items if p["entry_type"] == "role" and not p["text"].lstrip().startswith("["))
    project_items_text = "\n".join(
        f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
        for p in point_bank_items if p["entry_type"] == "project" and not p["text"].lstrip().startswith("["))
    system = (
        "You score one real candidate's fit against one real job posting -- not a vague "
        "impression, and not by literal keyword-counting -- and separately flag real gaps. "
        "You're given the candidate's role items (real work-experience accomplishments) and, "
        "separately, their project items -- each with a stable id. Do three things:\n\n"
        "1. REQUIREMENTS -- identify the 5 to 8 requirements from the posting that actually matter "
        "(hard skills, tools, years of experience, domain knowledge). Judge every one of these by the "
        "real scope and complexity of what the candidate actually did -- NEVER by a job title or level "
        "label (\"SDE-1\", \"Senior\", \"Staff\", ...) sitting next to it. A first job is never going to "
        "carry a senior title; that's just how careers start, not a sign the work itself was junior. If "
        "a role item describes real ownership, scale, or architectural judgment, credit that regardless "
        "of what the company happened to call the position.\n"
        "   When one JD bullet names two or more distinct skills (\"X and Y\", \"X/Y\", \"X or Y\"), give "
        "EACH one its own requirement line -- never fold them into a single requirement just because the "
        "posting listed them together. Confirmed live: folding \"JavaScript/TypeScript and Python\" into "
        "one \"Python skills\" requirement let a candidate with zero TypeScript earn full credit for it, "
        "because the missing half was never its own line to score \"no\" on. If the candidate is strong "
        "on one named skill and has nothing on the other, that MUST show up as two requirements with two "
        "different presence values, not one requirement averaged into looking satisfied. If splitting "
        "every compound bullet this way would push past 8 requirements, keep the split and let the "
        "count run a little over rather than re-merging two distinct skills back into one line just to "
        "hit the range -- 5 to 8 is a guide for how many distinct things usually matter, not a hard "
        "ceiling that overrides giving a real, separately-missed skill its own honest score.\n"
        "   For each requirement, judge:\n"
        "   - importance: \"required\" or \"nice_to_have\" (infer from phrasing -- \"must\"/\"required\" "
        "vs \"a plus\"/\"bonus\" -- if the posting doesn't label sections).\n"
        "   - presence: \"yes\" (a role item demonstrates it, named or clearly-transferable work -- "
        "e.g. JD wants \"Kubernetes\", candidate ran \"container orchestration at scale\" using it), "
        "\"semantic\" (a different but equivalent term -- JD wants \"React\", candidate shows "
        "\"React.js\"), or \"no\". Never mark \"semantic\"/\"yes\" for something only vaguely related -- "
        "that's still \"no\".\n"
        "   - depth: \"quantified\" (a real number/scale/named outcome backs it), \"named\" (mentioned "
        "with no quantified backing), or \"n/a\" if presence is \"no\".\n"
        "   - evidence_item_id: the id of the single role item that best demonstrates this "
        "requirement, or null if presence is \"no\".\n"
        "   - weight: how many of 100 total points this requirement is worth. HARD RULE, not a "
        "preference: every \"nice_to_have\" requirement's weight COMBINED must sum to 20 or less out of "
        "the 100 -- the remaining 80+ belongs to \"required\" requirements, split between them. A bonus "
        "the candidate happens to be strong on must never be able to outweigh a stated requirement; this "
        "cap is what guarantees that mathematically, not just by intent. If the posting is unusually "
        "soft and you genuinely find zero \"required\" requirements, don't force the 20-point cap "
        "anyway -- in that specific case nice_to_have requirements may split the full 100 among "
        "themselves, since the cap's whole purpose is protecting required requirements from being "
        "outweighed, and there's nothing to protect once none exist. Every requirement's weight "
        "together must still sum to EXACTLY 100 -- divide it out deliberately, don't just guess numbers "
        "that happen to be close.\n"
        "   - earned: how many of those weight points this requirement actually earns -- full weight "
        "only for presence \"yes\" with depth \"quantified\"; real but partial credit for \"semantic\" "
        "or \"named\" (judge how close, don't default to a fixed fraction); 0 for \"no\". This is what "
        "makes the final score auditable -- be honest and consistent, since these numbers are shown to "
        "the candidate directly as exactly where their score is coming from.\n\n"
        "2. PROJECTS -- separately, for each project item, judge whether it's genuinely worth adding "
        "to this specific resume: real relevance to this JD, or real standalone strength, vs. thin or "
        "redundant with the role experience already shown. This is advisory only -- your "
        "worth_including/reason here is a judgment call for the record, not a final inclusion "
        "decision (a candidate can still choose to force a project onto the resume regardless of your "
        "verdict).\n\n"
        "3. GAPS -- identify real weaknesses relative to this JD: either a requirement area with "
        "nothing in the role items that could honestly speak to it at all, OR one where there's "
        "something but it reads thin or junior for what this JD actually wants (a bare mention, not "
        "the depth/scale/ownership the posting implies). For each one, give: the gap itself, why what's "
        "there (if anything) isn't enough, and one concrete, realistic example of the kind of "
        "accomplishment that would close it -- specific enough to act on (what it would involve, roughly "
        "what scale/result would make it count), not generic career advice. This is a real, useful "
        "signal, not a failure to paper over.\n\n"
        "Write a two-sentence reason (the real gap if there is one -- required vs nice-to-have, stated "
        "plainly -- or the strongest match if there isn't, then one concrete thing that would close the "
        "gap or strengthen it further). Never cite a job title, level label, or years-of-tenure-at-one-"
        "company as the gap by itself -- if tenure genuinely falls short of a stated bar, say that "
        "plainly as its own real gap, but never phrase a title (\"SDE-1\", \"too junior a role\") as if "
        "the title itself were the deficiency. The weight/earned numbers above already determine the "
        "score -- you don't compute it separately, just make sure your reason is consistent with what "
        "they show.\n\n"
        "Reply with ONLY a JSON object: {\"requirements\": [{\"requirement\": str, \"importance\": str, "
        "\"presence\": str, \"depth\": str, \"evidence_item_id\": int|null, \"weight\": int, "
        "\"earned\": int}], \"project_assessment\": [{\"item_id\": int, \"worth_including\": bool, "
        "\"reason\": str}], \"missing\": [{\"gap\": str, \"why\": str, \"example\": str}], "
        "\"reason\": str}."
    )
    user = (
        f"Candidate: {profile.get('title', '')}, {candidate_years} years of real experience.\n\n"
        f"Role items (id | company -- role, dates):\n{role_items_text}\n\n"
        f"Project items (id | project -- tagline, dates):\n{project_items_text or '(none in the master doc yet)'}\n\n"
        f"Job posting -- {job['title']} at {job['company']}:\n{_trim_jd(job['description_raw'])}"
    )
    messages = [{"role": "user", "content": user}]
    by_id = {p["id"]: p for p in point_bank_items}
    reply, provider = _llm_json_call(system, messages, max_tokens=32000, temperature=0.2,
                                      **_llm_tuning(tuning_key))
    if reply is None:
        return None
    try:
        m = re.search(r"\{.*\}", reply, re.S)   # tolerate stray text/fences around the JSON
        obj = json.loads(m.group(0) if m else reply)
        reason = str(obj.get("reason", ""))
        if len(reason) > 700:
            cut = reason[:700]
            boundary = max(cut.rfind(" "), cut.rfind("\n"))
            reason = cut[:boundary] if boundary > 700 * 0.9 else cut
        requirements = []
        total_earned = 0
        for r in obj.get("requirements", []):
            if not isinstance(r, dict):
                continue
            try:
                weight = max(0, int(r.get("weight", 0)))
                earned = max(0, min(weight, int(r.get("earned", 0))))
            except (TypeError, ValueError):
                weight = earned = 0
            requirements.append({**r, "weight": weight, "earned": earned})
            total_earned += earned
        score = max(0, min(100, round(total_earned)))
        missing = []
        for g in obj.get("missing", []):
            if isinstance(g, dict):
                missing.append({"gap": str(g.get("gap", "")).strip(),
                                 "why": str(g.get("why", "")).strip(),
                                 "example": str(g.get("example", "")).strip()})
            else:   # tolerate a model that reverts to a flat string -- still show something real
                missing.append({"gap": str(g).strip(), "why": "", "example": ""})
        project_assessment = []
        for pa in obj.get("project_assessment", []):
            try:
                row = by_id.get(int(pa.get("item_id", -1)))
            except (TypeError, ValueError):
                continue   # non-numeric hallucinated id -- don't waste the whole call over one bad entry
            if row is None or row["entry_type"] != "project":
                continue
            project_assessment.append({"row_id": row["id"], "name": row["company"],
                                        "worth_including": bool(pa.get("worth_including")),
                                        "reason": str(pa.get("reason", "")).strip()})
    except Exception as e:
        print(f"[jobs] {provider} score-requirements parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return None
    return {
        "requirements": requirements,
        "missing": missing,
        "project_assessment": project_assessment,
        "score": score,
        "reason": reason,
    }


def _zai_select_content(job, profile, point_bank_items, skill_names, candidate_years, requirements,
                         forced_project_ids=None, advisory_project_ids=None, tuning_key="generate_batch"):
    """Call 2 of the resume judge/selector split: picks which role items go
    on THIS resume, truthfully rewords them, and picks skills -- grounded
    in Call 1's (_zai_score_requirements) already-computed `requirements`
    (read-only context here, not re-derived) so the two calls agree on what
    matters without redoing each other's work. Generation-only -- never
    called from _score_only, since score-only callers threw this exact work
    away unused every time.

    Role items get their own worth_including judgment here. Projects don't
    get re-judged here -- inclusion for a project is decided before this
    call, by one of two things: forced_project_ids (the user explicitly
    chose it via "Add project to resume") or advisory_project_ids (Call 1
    already judged it genuinely worth_including for this JD). Once a
    project is in either set, this call's only job for it is the same
    truthful reword/JD-vocabulary treatment role items get -- never a
    second worth-including judgment Call 1 already made. The two sets stay
    separate for the CALLER (only forced_project_ids is caller-protected
    from the one-page trim -- an advisory inclusion should be just as
    trimmable as any other normal item), but are treated identically here.
    forced_project_ids: project item ids the user explicitly chose to
    include via the "Add project to resume" button -- these DO land in
    priority_items regardless of Call 1's relevance judgment, appended
    after the role items; everything else stays bullets-only.

    Carries the SAME writing-quality disciplines the old combined call
    used -- HUMAN_STYLE_RULES baked into the prompt, AND a real code-level
    humanizer.check_human_style() gate on every included bullet's text,
    with up to 2 retries feeding violations back.

    `kind` (role vs project) is deliberately NOT asked of the model -- it's
    filled in afterward from the real DB row's entry_type, never trusted
    from the reply. priority_items is also defensively filtered: a project
    id only ever lands in it if it's in forced_project_ids, even if the
    model includes one anyway despite instructions -- the DB/caller decide
    what's allowed in, not the model's compliance with a prompt rule.

    Returns {"priority_items", "skills", "role_assessment"}, or None if the
    call/parse/style-gate failed."""
    role_items_text = "\n".join(
        f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
        for p in point_bank_items if p["entry_type"] == "role" and not p["text"].lstrip().startswith("["))
    requirements_text = "\n".join(
        f"- {r.get('requirement', '')} (importance={r.get('importance', '')}, weight={r.get('weight', 0)}, "
        f"evidence_item_id={r.get('evidence_item_id')})"
        for r in requirements) or "(none identified)"
    system = (
        "You build the tailored resume content for one real resume, against one real job posting -- "
        "truthful selection and wording only, never inventing. You're given the candidate's role items "
        "(real work-experience accomplishments), and this posting's already-scored requirements "
        "(computed separately -- treat them as given, don't re-derive or second-guess them). Do two "
        "things:\n\n"
        "1. CONTENT SELECTION -- bullets only, from the role items. For EVERY role item, give an "
        "explicit verdict: worth_including (true/false) and a one-sentence reason, even for items "
        "you're excluding -- this is the audit trail for why each item did or didn't make the cut, not "
        "just the final list. Two reasons an item earns a place: it's the evidence for a real "
        "requirement above, or it's simply a strong, impressive accomplishment worth showing even "
        "without a direct JD match (a one-page resume with room to spare should never go half-empty "
        "rather than show real strength). Order every item you're including by real value for THIS "
        "resume, strongest first: requirement-evidence items (by how well they satisfy a required vs. "
        "nice-to-have requirement) before pure-backfill items. Don't hold back for page-length reasons "
        "-- include everything genuinely worth showing; a separate, real page-count check downstream "
        "trims the list if it runs long, worst-value items first, by the order you give.\n"
        "   For EVERY item you're including, run TWO separate, independent checks before ever "
        "considering whether to omit the text field -- these are two different reasons to reword, not "
        "one, and a pass on either one alone is NOT enough to skip rewording:\n"
        "   CHECK A (JD vocabulary): does the JD name a specific tool, technique, or concept that this "
        "item genuinely, truthfully demonstrates but describes in different words? If yes, reword to "
        "use the JD's term. This also covers the JD's own REPEATED framing/theme words, not just named "
        "tools -- if the JD's title or opening paragraph centers on a word like \"distributed systems\", "
        "\"real-time\", or \"high-throughput\" and an item's real, true work genuinely fits that framing "
        "(e.g. a large multi-pipeline data platform really is a distributed system, even if the point-"
        "bank text never used that exact word), use the JD's own word for it. Checked live: a real "
        "generate for a posting titled \"Distributed Systems\" that used the word four times never used "
        "it once anywhere in the resume or cover letter, despite the underlying work genuinely "
        "qualifying -- that's a missed match, not appropriate restraint.\n"
        "   CHECK B (plain language -- the one that gets skipped if you're not deliberate about it): "
        "read the item's ORIGINAL point-bank wording as if you'd never seen the system before. Would "
        "someone outside this exact specialty follow what was done and why it mattered on one read? "
        "Point-bank text is written for the candidate's own records, not for a stranger skimming for a "
        "few seconds -- it is allowed to be denser or more jargon-heavy than a resume should be, and "
        "the fact that it's already on file is NOT evidence it's already plain enough. If it leans on "
        "unexplained acronyms, stacked technical nouns, or a sentence that needs a second read, reword "
        "it in plainer words even though the facts and the JD-match don't change. This check fires on "
        "most items in a real resume, not a rare few -- if you are about to leave most of the role "
        "items unchanged, go back and actually re-read each original sentence as a stranger would "
        "before concluding it was already plain.\n"
        "   The text field may ONLY be omitted when BOTH checks come back negative -- no JD-vocabulary "
        "swap needed AND the original wording is already plain enough for a non-specialist to follow in "
        "one read. If either check is positive, you must write out the real reworded text, never retype "
        "the original unchanged and never omit it. Never add a fact, number, or outcome that isn't "
        "already there -- only reword what's genuinely, truthfully present. Every bullet, reworded or "
        "not, must carry a real named skill/technology and how it was used, and/or a measurable result "
        "(a number, a named outcome, an award) -- never a bare responsibility with nothing backing it "
        "(don't open on \"Implemented X\" or \"Worked on Y\" with nothing else). The goal is a reader "
        "immediately grasping the impact in a few seconds, not a display of domain vocabulary -- if a "
        "technical term is genuinely the right word, the surrounding words still have to carry the real "
        "accomplishment on their own. And any wording you do write must follow these rules exactly:\n"
        + HUMAN_STYLE_RULES + "\n"
        "   Separately, if the user message below lists any Project items to include, each one is "
        "already decided -- either the candidate explicitly chose it, or a separate earlier judgment "
        "already found it genuinely worth including on this resume. For each, decide only whether to "
        "reword it (same truthful-reword/JD-vocabulary rule and style rules as role items above) and "
        "report it under forced_project_items; you are not judging whether to include it, only how to "
        "word it. The plain-language rule above applies especially hard here -- project "
        "bullets tend to carry the densest jargon (model/framework names, ML technique acronyms), and "
        "a reader who doesn't already know that specific stack should still come away understanding "
        "what was actually built and why it was hard, not just see a wall of technology names.\n\n"
        "2. SKILLS -- from the candidate's tagged skills given below, pick the ones genuinely relevant "
        "to this JD. Only include a \"display\" value when the JD's own exact term is a truthful match "
        "for something the candidate actually tagged and reads better than the original (e.g. "
        "candidate has \"REST APIs\" tagged, JD says \"APIs\" -- display \"APIs\"); leave it out "
        "entirely when the original tag text is already fine, the caller uses the original name by "
        "default. Never invent a skill the candidate didn't tag. Order by relevance to this JD, most "
        "relevant first.\n\n"
        "Reply with ONLY a JSON object: {\"role_assessment\": [{\"item_id\": int, \"worth_including\": "
        "bool, \"reason\": str, \"text\": str (OMIT this field entirely, don't send an empty string, "
        "when you're not changing the item's wording)}], \"forced_project_items\": [{\"item_id\": int, "
        "\"text\": str (OMIT when not changing wording)}] (empty list if none were listed), "
        "\"skills\": [{\"name\": str, \"display\": str (OMIT when the original tag name is already "
        "fine)}]}."
    )
    forced = set(forced_project_ids or [])
    include_ids = forced | set(advisory_project_ids or [])
    forced_note = ""
    if include_ids:
        forced_rows = [p for p in point_bank_items if p["entry_type"] == "project" and p["id"] in include_ids]
        forced_text = "\n".join(
            f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
            for p in forced_rows)
        forced_note = f"\n\nProject items to include (id | project -- tagline, dates):\n{forced_text}"
    user = (
        f"Candidate: {profile.get('title', '')}, {candidate_years} years of real experience.\n\n"
        f"Role items (id | company -- role, dates):\n{role_items_text}\n\n"
        f"Already-scored requirements for this posting (treat as given):\n{requirements_text}\n\n"
        f"Known skills (tags only -- no depth signal by themselves): {', '.join(skill_names)}\n\n"
        f"Job posting -- {job['title']} at {job['company']}:\n{_trim_jd(job['description_raw'])}"
        + forced_note
    )
    messages = [{"role": "user", "content": user}]
    known_lower = {s.lower() for s in skill_names}
    by_id = {p["id"]: p for p in point_bank_items}
    for attempt in range(3):
        reply, provider = _llm_json_call(system, messages, max_tokens=32000, temperature=0.2,
                                          **_llm_tuning(tuning_key))
        if reply is None:
            return None
        try:
            m = re.search(r"\{.*\}", reply, re.S)   # tolerate stray text/fences around the JSON
            obj = json.loads(m.group(0) if m else reply)
            priority_items = []
            role_assessment = []
            for it in obj.get("role_assessment", []):
                try:
                    row = by_id.get(int(it["item_id"]))
                except (TypeError, ValueError):
                    # Non-numeric id (seen live: "93_alt_unused") -- the
                    # model inventing a variant id, never a real row.
                    # Skip it the same as any other hallucinated id,
                    # don't let one bad entry crash the whole parse.
                    continue
                if row is None or row["entry_type"] != "role":   # hallucinated/stale id, or not a role item -- skip
                    continue
                worth = bool(it.get("worth_including"))
                role_assessment.append({"row_id": row["id"], "worth_including": worth,
                                         "reason": str(it.get("reason", "")).strip()})
                if worth:
                    text = str(it.get("text") or "").strip() or row["text"]
                    priority_items.append({"row_id": row["id"], "text": text, "kind": row["entry_type"]})
            for it in obj.get("forced_project_items", []):
                try:
                    row = by_id.get(int(it["item_id"]))
                except (TypeError, ValueError):
                    continue   # non-numeric hallucinated id -- same tolerance as role_assessment above
                if row is None or row["entry_type"] != "project" or row["id"] not in include_ids:
                    continue   # projects only land here if forced or Call-1-approved -- enforced here, not trusted from the prompt
                text = str(it.get("text") or "").strip() or row["text"]
                priority_items.append({"row_id": row["id"], "text": text, "kind": row["entry_type"]})
            if not priority_items:
                print(f"[jobs] {provider} select-content returned zero usable priority_items for "
                      f"job {job['id']}", file=sys.stderr)
                return None
            skills = []
            for s in obj.get("skills", []):
                name = str(s.get("name", "")).strip()
                if name.lower() not in known_lower:   # never show a skill the candidate didn't actually tag
                    continue
                skills.append({"name": name, "display": str(s.get("display", name) or name).strip()})
        except Exception as e:
            print(f"[jobs] {provider} select-content parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
            return None
        violations = []
        for it in priority_items:
            for v in jobs_humanizer.check_human_style(it["text"]):
                violations.append(f'bullet "{it["text"][:60]}...": {v}')
        if not violations:
            return {
                "priority_items": priority_items,
                "skills": skills,
                "role_assessment": role_assessment,
            }
        if attempt < 2:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": (
                "Some bullets fail the style check -- fix these and reply with the same JSON shape, "
                "nothing else:\n- " + "\n- ".join(violations))})
    # Last attempt still has violations -- one stubborn bullet used to fail the ENTIRE call,
    # throwing away 9 good reworded items because of 1 the model couldn't fix in 3 tries.
    # Found live on a real generate. Per-item salvage instead: revert ONLY the still-
    # violating items to their real, untouched point-bank text (which is independently
    # checked -- if the ORIGINAL also fails, that one item is dropped, never shipped
    # unchecked) and keep every other item's successfully reworded text. A slightly-
    # unpolished real bullet beats failing the whole resume.
    salvaged, dropped = [], []
    for it in priority_items:
        if jobs_humanizer.check_human_style(it["text"]):
            row = by_id.get(it["row_id"])
            original = row["text"] if row is not None else ""
            if original and not jobs_humanizer.check_human_style(original):
                salvaged.append({**it, "text": original})
            else:
                dropped.append(it["row_id"])
        else:
            salvaged.append(it)
    if dropped:
        print(f"[jobs] {provider} select-content: dropped row_ids {dropped} for job {job['id']} -- "
              "neither the reworded nor the original text passed the style check", file=sys.stderr)
    if not salvaged:
        print(f"[jobs] {provider} select-content failed the humanizer gate 3x for job {job['id']}, "
              "nothing salvageable", file=sys.stderr)
        return None
    return {"priority_items": salvaged, "skills": skills, "role_assessment": role_assessment}


def _cached_score_for_job(job):
    """The job's already-judged Call 1 result, in the exact shape
    _zai_score_requirements returns, or None if it's never been judged or
    was judged before the master doc (point bank/skills/profile) last
    changed -- same rule jobs_llm_score_stale already enforces for the
    AI-judge button, reused here so generate() can skip a redundant,
    independent re-score instead of silently producing a second, possibly
    different, number for the same posting."""
    if jobsdb.jobs_llm_score_stale(job):
        return None
    requirements = json.loads(job["llm_requirements"] or "[]")
    if not requirements:
        # A job scored before llm_requirements existed (or scored by a code
        # path that predates this reuse) has a real, non-stale score but no
        # requirement grounding to hand Call 2 -- not safe to reuse as-is.
        # Falling through to a real re-score here is a one-time cost per
        # such job; once it has, this field is populated and every later
        # generate reuses it normally.
        return None
    return {
        "score": job["llm_score"],
        "reason": job["llm_reason"],
        "requirements": requirements,
        "missing": json.loads(job["llm_missing"] or "[]"),
        "project_assessment": json.loads(job["llm_projects"] or "[]"),
    }


def _zai_generate_and_score(job, profile, point_bank_items, skill_names, candidate_years, forced_project_ids=None,
                             tuning_key="generate_batch"):
    """Thin orchestrator over the two-call split above: _zai_score_requirements
    (Call 1 -- requirements/score/gaps/project worth_including) then
    _zai_select_content (Call 2 -- role-item selection/reword/skills,
    grounded in Call 1's requirements). Same name and return shape as the
    old single-call version this replaced, so every caller (jobsearch's
    generate.generate(), _run_generate_batch, the generate-queued/
    generate-with-project endpoints) needed zero changes. Two distinct
    callers: (1) score-only (AI-judge batch, single-job rescore) -- calls
    _zai_score_requirements directly instead (see _score_only), never this
    function, since it never needed Call 2's work in the first place; (2)
    generate -- calls this, and hands priority_items to jobsearch's
    generate.generate() to actually render.

    tuning_key: which LLM_TUNING row both calls run under -- see
    LLM_TUNING for what it resolves to (model tier + reasoning effort).

    Returns a dict ready to hand straight to jobsearch's generate.generate()
    as `selection`, or None if either call/parse/style-gate failed (caller
    treats that as "not generated yet", never as a 0 or an empty resume).

    Reuses the job's already-judged Call 1 result (score/reason/requirements/
    missing/project_assessment) when it isn't stale, instead of always
    re-running Call 1 here -- found live: re-running it unconditionally meant
    generate() and the AI-judge batch each independently called the same
    non-deterministic scoring prompt, so the postings list and a resume's own
    ats_estimate.md could show two different scores for the identical job,
    with no real change in between. Same staleness rule the AI-judge button
    itself already uses (jobs_llm_score_stale), so a master-doc edit still
    forces a genuine re-score here, same as it always has."""
    scored = _cached_score_for_job(job)
    if scored is None:
        scored = _zai_score_requirements(job, profile, point_bank_items, candidate_years, tuning_key=tuning_key)
        if scored is None:
            return None
    # A project Call 1 already judged genuinely worth_including for this JD now actually
    # lands on the resume instead of sitting advisory-only until the user manually clicks
    # "Add to resume" -- found live: a real generate for a distributed-systems posting
    # produced zero Projects section even though Kapitsa was real, relevant, true work.
    # Kept separate from forced_project_ids below (not trim-protected) since this is a
    # relevance call, not something the user explicitly insisted on.
    #
    # Explicitly Kapitsa only -- told directly: exactly one project on the resume, and it's
    # Kapitsa, never Curie (a tool that writes resumes showing up as a bullet ON a resume
    # reads oddly, and Kapitsa is the stronger, more complete story). Filtered here, not
    # left to Call 1's own relevance judgment, since Call 1 has in the past judged Curie
    # worth_including for some postings -- that's no longer a judgment call this makes.
    advisory_project_ids = {pa["row_id"] for pa in scored["project_assessment"]
                             if pa["worth_including"] and pa["name"] == "Kapitsa"}
    # Separately, always OFFER the current Kapitsa row as a low-priority fill candidate
    # too, even when Call 1 said it's not core-relevant -- real empty space at the bottom
    # of a page with zero worth_including projects just stayed empty, because a project
    # never got a chance to be considered unless Call 1 approved it outright. This is
    # deliberately NOT added to advisory_project_ids (so generate.py's trim-protection
    # stays limited to genuine approvals) -- a fallback filler should only survive when
    # there's real spare room, same real-rendered-page-count logic the one-page trim
    # already uses, never promoted to "must survive even under page pressure."
    kapitsa_rows = [p for p in point_bank_items
                    if p["entry_type"] == "project" and p["company"] == "Kapitsa"]
    fallback_project_ids = {max(kapitsa_rows, key=lambda r: r["id"])["id"]} if kapitsa_rows else set()
    # If the caller didn't explicitly force a project (the normal "just click Generate"
    # path -- no one clicked "Add project to resume"), but Call 1 genuinely judged Kapitsa
    # worth including for THIS JD, promote that into the forced set so it actually survives
    # the one-page trim. Found live, repeatedly: the default path kept producing resumes
    # with zero Projects section even when Call 1 had approved Kapitsa, because merely
    # "advisory" inclusion is NOT trim-protected -- a prior version of this code deliberately
    # left it that way, reasoning an approval shouldn't out-rank role bullets by default, but
    # the standing instruction here is "add Kapitsa whenever it's genuinely relevant," not
    # "only if the user remembers to force it in."
    effective_forced_ids = set(forced_project_ids or ()) or set(advisory_project_ids)
    selected = _zai_select_content(job, profile, point_bank_items, skill_names, candidate_years,
                                    scored["requirements"], forced_project_ids=effective_forced_ids,
                                    advisory_project_ids=advisory_project_ids | fallback_project_ids,
                                    tuning_key=tuning_key)
    if selected is None:
        return None
    priority_items = selected["priority_items"]
    # "Exactly one project on the resume" is the rule -- but Call 2 is offered
    # forced_project_ids AND advisory_project_ids as separate candidate lists, and
    # nothing stopped it from including more than one if it judged several relevant.
    # Found live: a real run included both the forced Kapitsa row and the fallback
    # Kapitsa row, inflating the Projects section to 2 bullets and squeezing role
    # bullets out of the one-page budget. Hard cap at one here, preferring whichever
    # row was actually forced (an explicit user insistence beats the model's own pick).
    project_items = [it for it in priority_items if it["kind"] == "project"]
    if len(project_items) > 1:
        keep = next((it for it in project_items if it["row_id"] in effective_forced_ids), project_items[0])
        priority_items = [it for it in priority_items if it["kind"] != "project"] + [keep]
    # Offering a candidate to Call 2 only works if the model actually bothers to emit it --
    # it's told "you are not judging whether to include it, only how to word it" for every
    # offered id, but that's a request, not a guarantee. Found live: a real run with real
    # spare page room (both companies well under the cap) still produced zero project
    # items, because the model's reply simply never mentioned one despite candidates being
    # listed. Deterministic backstop: if nothing with kind="project" made it through at
    # all, append the best available project's real, untouched point-bank text directly --
    # same spirit as _balance_role_bullets' backfill, a guarantee that doesn't depend on
    # the model choosing to cooperate.
    if not any(it["kind"] == "project" for it in priority_items):
        by_id = {p["id"]: p for p in point_bank_items}
        fallback_id = next(iter(advisory_project_ids), None) or next(iter(fallback_project_ids), None)
        row = by_id.get(fallback_id)
        # Still goes through the same style gate every other bullet does -- this bypasses
        # Call 2's own already-passed violations check (the raw text was never part of that
        # reply), so it gets its own check rather than trusting unchecked text onto the page.
        if row is not None and not jobs_humanizer.check_human_style(row["text"]):
            priority_items = priority_items + [{"row_id": row["id"], "text": row["text"], "kind": "project"}]
    return {
        "priority_items": priority_items,
        "requirements": scored["requirements"],
        "project_assessment": scored["project_assessment"],
        "skills": selected["skills"],
        "missing": scored["missing"],
        "score": scored["score"],
        "reason": scored["reason"],
        # Carried through so generate.py's page-trim never silently drops a project the
        # user explicitly forced on via "Add to resume" -- OR one Call 1 itself judged
        # genuinely worth including, now promoted into this same set above.
        "forced_project_ids": list(effective_forced_ids),
        # Genuine Call-1 approvals only (never the fallback-filler ids) --
        # generate.py uses this to decide which project, if any, earns the
        # "survive the one-page trim" floor. A fallback filler never does;
        # it only ever shows up when there's real spare room.
        "advisory_project_ids": list(advisory_project_ids),
    }


def _points_text_for_selection(point_bank_items, priority_items):
    """Scoped points_text for _zai_draft_application -- only the items
    actually selected for the resume (priority_items), using each item's
    FINAL text (reworded by Call 2, or original if unchanged). Fixes the
    cover letter being able to reference a fact the resume doesn't include:
    it used to be drafted from the entire point bank, before content
    selection had even run."""
    by_id = {p["id"]: p for p in point_bank_items}
    lines = []
    for it in priority_items:
        row = by_id.get(it["row_id"])
        if row is None:
            continue
        lines.append(f"[{row['company']} -- {row['role']}, {row['start_date']} to "
                      f"{row['end_date'] or 'present'}] {it['text']}")
    return "\n".join(lines)


def _zai_draft_application(job, profile, points_text, skill_names, candidate_years,
                            tuning_key="generate_batch"):
    """Writes a resume summary + cover letter body for one job, grounded
    strictly in the real point bank -- never invents a fact, number, or
    accomplishment. Returns (summary, cover_letter_body, None) on success,
    or (None, None, reason) on failure -- reason distinguishes "the Zai
    call itself failed" (no key, rate-limited, network) from "it replied
    but never passed the humanizer gate in 3 tries", since those need
    different responses (wait and retry vs. look at what it kept writing).

    Resolves the tension the original hand-written-only design existed to
    avoid: a blind LLM call producing generic "I am excited to apply..."
    AI-slop. The fix isn't "no LLM", it's a real enforced gate -- every
    draft is checked with humanizer.check_human_style() (the same hard
    gate generate() itself refuses to render past) before it's accepted,
    with up to 2 retries feeding the exact violations back if it fails."""
    system = (
        "You write a tailored resume summary and cover letter for one real job posting, "
        "grounded ONLY in the candidate's real accomplishments given below. Never invent a "
        "fact, number, project, or outcome not literally present in the point bank text -- "
        "if the posting wants something the point bank doesn't cover, don't claim it, just "
        "don't mention it. Be specific: reference real numbers and real systems from the "
        "point bank, and real specifics from the posting (the team, the problem, a named "
        "technology) -- not generic enthusiasm. If the posting's own title or opening "
        "paragraph repeats a theme word (e.g. \"distributed systems\", \"real-time\", "
        "\"high-throughput\") and the candidate's real work genuinely fits that framing, use "
        "the posting's own word for it -- don't describe the same true work in different "
        "language just because the point bank happened to phrase it differently.\n\n"
        + HUMAN_STYLE_RULES +
        "\nResume summary: exactly 2 sentences, no greeting, same shape every time so it reads "
        "as one consistent, deliberate statement rather than a different structure each run:\n"
        "   Sentence 1: years of real experience + the single most impressive, concrete scale "
        "fact from the point bank (a real number -- data volume, request rate, pipeline count, "
        "whatever is strongest), tied to whichever company it actually happened at.\n"
        "   Sentence 2: one more concrete differentiator from a DIFFERENT accomplishment or "
        "company than sentence 1 used, picked for relevance to THIS posting -- never repeat "
        "the same fact sentence 1 already used.\n"
        "   TENSE, checked against the real dates on each point-bank item (every item is "
        "tagged with its own start/end date): if the role's end date is a real date in the "
        "past, every accomplishment from it is PAST tense ONLY -- \"built\", \"owned\", \"cut\", "
        "\"drove\" -- never \"I build\", \"I own\", \"I run\", which claims the candidate is "
        "currently doing it. Only a role with no end date (still ongoing) may use present "
        "tense. Check every single role used in the summary before writing -- if ALL the roles "
        "referenced have already ended, the ENTIRE summary is past tense, with no exceptions "
        "and no mixing -- writing \"I build X\" about a job that ended months ago misstates the "
        "candidate's current employment status, which is a real factual error, not a style "
        "choice. Pick ONE clear, concrete way to describe what the candidate actually did in "
        "each accomplishment -- never blend two different framings in the same sentence (e.g. "
        "\"owned the pipelines\" and \"built the platform other teams used\" are two different "
        "claims; pick whichever is literally true and say only that, plainly, so a reader isn't "
        "left guessing which one it actually was). Tone is warm and a little humble -- never "
        "phrase anything as the candidate being able to do what others can't, or any other "
        "comparison that puts them above unnamed others; let the real facts make the case "
        "instead of a claim of superiority.\n"
        "Cover letter body: 3-4 short paragraphs, no \"Dear Hiring Manager\" greeting or "
        "sign-off (the renderer adds those), grounded in specific real point-bank facts "
        "connected to specific things this posting actually asks for.\n\n"
        "Reply with ONLY a JSON object: {\"summary\": str, \"cover_letter_body\": str} "
        "(cover_letter_body paragraphs separated by a blank line, i.e. \\n\\n)."
    )
    user = (
        f"Candidate: {profile.get('title', '')}, {candidate_years} years of real experience.\n\n"
        f"Real accomplishments, with company/role/dates:\n{points_text}\n\n"
        f"Known skills: {', '.join(skill_names)}\n\n"
        f"Job posting -- {job['title']} at {job['company']}:\n{_trim_jd(job['description_raw'])}"
    )
    messages = [{"role": "user", "content": user}]
    # 4, was 3 -- this call has no salvage fallback (unlike bullet selection, which can
    # revert to the real point-bank text when a reword keeps failing). A style-gate loss
    # here kills the whole generation outright, so it gets one extra attempt with the
    # explicit violations fed back before giving up.
    draft_attempts = 4
    for attempt in range(draft_attempts):
        reply, provider = _llm_json_call(system, messages, max_tokens=32000, temperature=0.4,
                                          **_llm_tuning(tuning_key))
        if reply is None:
            return None, None, "LLM call failed on both Zai and Claude -- see server logs (often a rate limit; check for a 429)"
        try:
            m = re.search(r"\{.*\}", reply, re.S)
            obj = json.loads(m.group(0) if m else reply)
            summary, cover = str(obj["summary"]).strip(), str(obj["cover_letter_body"]).strip()
        except Exception as e:
            print(f"[jobs] {provider} draft parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
            return None, None, f"{provider} reply wasn't valid JSON: {e}"
        violations = (jobs_humanizer.check_human_style(summary)
                      + jobs_humanizer.check_human_style(cover))
        # An empty/near-empty string trivially passes every style check
        # above (there's nothing in it to flag) -- found live: a real
        # resume went out with a blank Summary section because this gate
        # never actually required non-empty content, just "no bad
        # patterns in whatever's there, including nothing."
        if len(summary) < 20:
            violations.append("summary is empty or too short -- write the real 1-2 sentence summary, not nothing")
        if len(cover) < 50:
            violations.append("cover_letter_body is empty or too short -- write the real 3-4 paragraph cover letter, not nothing")
        if not violations:
            return summary, cover, None
        if attempt < draft_attempts - 1:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": (
                "That draft fails the style check -- fix these and reply with the same "
                "JSON shape, nothing else:\n- " + "\n- ".join(violations))})
    reason = f"failed the humanizer gate {draft_attempts}x in a row: " + "; ".join(violations)
    print(f"[jobs] {provider} draft for job {job['id']} {reason}", file=sys.stderr)
    return None, None, reason


def _zai_rewrite_point(raw_text, company, role):
    """Reformats ONE point-bank bullet into Keyword+Use+Result shape --
    never invents. If a real brag/result is already present just phrased
    badly, rewrite it; if it genuinely isn't there, refuse and say exactly
    what's missing instead of making one up.

    Returns a dict: {"status": "rewritten", "text": str} or
    {"status": "missing_info", "explanation": str, "question": str}, or
    None if the Zai call itself failed (caller should treat that as "try
    again later", not as a real refusal)."""
    system = (
        "You reformat ONE real resume bullet point into a stronger shape, without ever "
        "inventing anything. Never add a fact, number, outcome, or detail that isn't "
        "literally present in the text given to you -- if you're tempted to guess a number "
        "or soften a gap with a vague phrase, stop and refuse instead.\n\n"
        "The target shape: name the real skill/technology, how it was actually used, and "
        "the measurable result or named outcome -- one bullet, one or two sentences, no "
        "paragraph.\n\n"
        "If the given text already contains a real result (a number, a named outcome, an "
        "award, concrete feedback) but is phrased awkwardly or buries it, rewrite it into "
        "that shape, preserving every real fact and number exactly as given.\n\n"
        "If the given text has NO real result or outcome anywhere in it -- a bare activity "
        "or responsibility with nothing measurable or named -- do NOT invent one. Instead "
        "explain in one sentence what's missing, then ask ONE specific, concrete question "
        "that would help recall the real missing fact (a number, a before/after, who "
        "noticed, what changed).\n\n"
        "No AI-sounding buzzwords (delve, leverage, underscore, boast, robust, seamless), "
        "no em dashes.\n\n"
        "Reply with ONLY a JSON object, one of:\n"
        '{"status": "rewritten", "text": "<the rewritten bullet>"}\n'
        '{"status": "missing_info", "explanation": "<one sentence, what\'s missing>", '
        '"question": "<one specific question to help recall it>"}'
    )
    user = f"Company/role: {company} / {role}\n\nBullet as written:\n{raw_text}"
    reply, provider = _llm_json_call(system, [{"role": "user", "content": user}], max_tokens=32000, temperature=0.3,
                                      **_llm_tuning("rewrite_point"))
    if reply is None:
        return None
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        if obj.get("status") == "rewritten":
            return {"status": "rewritten", "text": str(obj["text"]).strip()}
        return {"status": "missing_info", "explanation": str(obj.get("explanation", "")).strip(),
                "question": str(obj.get("question", "")).strip()}
    except Exception as e:
        print(f"[jobs] {provider} point-rewrite parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return None


# Used by the on-demand "Tidy master doc" button: given everything
# already on file for ONE company, return the cleaned-up complete list --
# same real accomplishment told as 2+ points gets combined into one,
# carrying every real detail; genuinely distinct points come back
# untouched. The caller (see /api/profile/points/tidy) diffs the result
# against what's on file by plain text match -- the model only ever
# handles text in, text out, no ids to track or get wrong.
def _zai_tidy_company(company, role, existing_texts):
    """Returns {"points": [str, ...], "notes": [str, ...]} -- the complete
    point list for this company after cleanup, and anything worth flagging
    (e.g. a factual conflict between two merged points). None on a call/
    parse failure (caller treats that as 'couldn't check, leave as-is')."""
    items_text = "\n".join(existing_texts)
    system = (
        "You clean up a candidate's real accomplishment points for ONE company/role. You're given "
        "every point currently on file for it.\n\n"
        "Return the complete list of points for this company after cleanup: combine any two or more "
        "points that describe the SAME real accomplishment into one bullet carrying every real fact, "
        "number, and detail from all of them. Never invent anything not literally present in the "
        "input, and never drop a real detail just because only one of the combined points mentioned it.\n\n"
        "Two points are the same accomplishment if they're about the same system, project, or event -- "
        "even if worded completely differently. They are NOT the same just because they share a "
        "technology or domain (two different features of the same system are two different "
        "accomplishments, not one).\n\n"
        "If two points disagree on a number or fact for the same accomplishment, keep the more "
        "specific/complete one and say so in notes -- never silently drop the discrepancy.\n\n"
        "For any point that doesn't need combining with anything, return it completely unchanged, "
        "character for character -- don't reword, polish, or restructure something that isn't "
        "actually being merged.\n\n"
        "Any point you DO rewrite as part of a merge must follow this shape: name the real "
        "skill/technology, how it was actually used, and the measurable result -- one bullet, one or "
        "two sentences, no paragraph. No AI-sounding buzzwords (delve, leverage, underscore, boast, "
        "robust, seamless), no em dashes.\n\n"
        "Reply with ONLY a JSON object: {\"points\": [str, ...], \"notes\": [str, ...]} -- notes lists "
        "anything worth flagging (e.g. a conflict you resolved), empty if nothing to flag."
    )
    user = f"Company/role: {company} / {role}\n\nExisting points:\n{items_text}"
    reply, provider = _llm_json_call(system, [{"role": "user", "content": user}], max_tokens=32000, temperature=0.2,
                                      **_llm_tuning("rewrite_point"))
    if reply is None:
        return None
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        return {"points": [str(t).strip() for t in obj.get("points", [])],
                "notes": [str(n).strip() for n in obj.get("notes", [])]}
    except Exception as e:
        print(f"[jobs] {provider} tidy parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return None


# AI-judge, async: a real batch over "all" stale/new candidates is however
# many real LLM calls that is (seconds each) -- blocking the HTTP request
# for that risked a timeout the moment the candidate set got large.
#
# Score-only mode: calls the exact same _zai_score_requirements generation
# would use, so the pre-generation score is never a different number for a
# different reason than the real one -- one judge, used consistently,
# whether scoring one posting, all of them, or actually generating.
_judge_lock = threading.Lock()
# Loaded from disk, not just started blank -- survives a server restart.
# If the persisted state says a batch was "running", the process that was
# running it is gone (this is a fresh process starting up) -- correcting
# that to "interrupted" here is the one honest thing to do with it.
_judge_progress = jobsdb.judge_progress_get() or {
    "running": False, "total": 0, "done": 0, "judged": 0, "failed": [], "started_ts": None}
if _judge_progress.get("running"):
    _judge_progress["running"] = False
    _judge_progress["interrupted"] = True


def _judge_progress_update(**kwargs):
    """The one place _judge_progress changes -- updates the in-memory copy
    and writes it straight through to disk in the same call, so every
    real change (not just the final one) is recoverable after a restart."""
    _judge_progress.update(kwargs)
    jobsdb.judge_progress_set(_judge_progress)


def _score_only(job, profile, point_bank_items, candidate_years, tuning_key="judge_batch"):
    """Runs _zai_score_requirements (Call 1 only -- scoring never needs
    Call 2's content-selection work, which score-only callers always threw
    away unused), writes the score-relevant fields onto the job row.
    Returns (True, None) if a real score landed, (False, reason) if the
    call/parse failed (caller treats that as "not scored yet", same
    discipline as before) -- a real reason, not just a bool.

    tuning_key: "judge_batch" (default, the AI-judge batch loop) or
    "rescore_single" (the single-job /api/jobs/rescore endpoint) -- see
    LLM_TUNING."""
    result = _zai_score_requirements(job, profile, point_bank_items, candidate_years, tuning_key=tuning_key)
    if result is None:
        return False, "LLM call failed or returned unusable content -- see server logs"
    jobsdb.jobs_set_llm_score(job["id"], result["score"], result["reason"],
                               missing=result["missing"], projects=result["project_assessment"],
                               requirements=result["requirements"])
    return True, None


def _cluster_missing_gaps(scored_jobs):
    """Every scored job's llm_missing is free text from a separate LLM call
    -- "No demonstrated Python depth" and "No production Python or Golang
    experience" are the same real gap, worded differently, and a plain
    string/keyword match won't see that. One LLM call groups the real,
    raw gaps (tagged with which posting each came from) into distinct
    underlying weaknesses -- semantically distinct, not textually distinct.
    Sorted by how many postings actually hit it, in Python (trust the
    count's arithmetic, not the model's), so the most-common real gap
    leads. Returns [] if there's nothing to cluster or the call failed --
    an empty tab is honest here, not an error."""
    raw = []
    for j in scored_jobs:
        try:
            missing = json.loads(j["llm_missing"] or "[]")
        except Exception:
            missing = []
        for m in missing:
            gap = (m.get("gap") or "").strip()
            if gap:
                raw.append({"job_id": j["id"], "gap": gap})
    if not raw:
        return []
    items_text = "\n".join(f"[job_id={r['job_id']}] {r['gap']}" for r in raw)
    system = (
        "You're given real skill/experience gaps flagged against a candidate across many real job "
        "postings, each tagged with which posting it came from. Group them into distinct underlying "
        "weaknesses -- gaps about the same real skill or experience area belong in the same group, "
        "even if worded completely differently (e.g. \"No demonstrated Python depth\" and \"No "
        "production Python or Golang experience\" are the SAME gap). Don't force genuinely different "
        "gaps together just because they're both vaguely technical (\"no Python\" and \"no Rust\" are "
        "different groups). For each group, give a short, clear, plain-English label naming the real "
        "gap -- not a copy of one entry's wording, a real synthesis -- and the job_ids of every "
        "posting that reported something in this group (a posting can only appear in a group once, "
        "even if it had multiple similar entries).\n\n"
        "Reply with ONLY a JSON object: {\"clusters\": [{\"label\": str, \"job_ids\": [int, ...]}]}."
    )
    user = f"Gaps:\n{items_text}"
    reply, provider = _llm_json_call(system, [{"role": "user", "content": user}], max_tokens=32000, temperature=0.1,
                                      **_llm_tuning("gap_cluster"))
    if reply is None:
        return []
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        valid_ids = {j["id"] for j in scored_jobs}
        clusters = []
        for c in obj.get("clusters", []):
            label = str(c.get("label", "")).strip()
            job_ids = sorted({int(jid) for jid in c.get("job_ids", []) if int(jid) in valid_ids})
            if label and job_ids:
                clusters.append({"label": label, "count": len(job_ids), "job_ids": job_ids})
        clusters.sort(key=lambda c: c["count"], reverse=True)
        return clusters
    except Exception as e:
        print(f"[jobs] {provider} gap-clustering parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return []


def _run_judge_batch(candidates, profile, point_bank_items, candidate_years):
    """Returns False without starting anything if a batch is already
    running -- one at a time app-wide, so two clicks don't double-spend
    on the same candidates."""
    if _judge_progress["running"]:
        return False
    _judge_progress_update(running=True, total=len(candidates), done=0,
                            judged=0, failed=[], started_ts=time.time(), interrupted=False)

    def run():
        with _judge_lock:
            try:
                for j in candidates:
                    try:
                        fresh = jobsdb.jobs_get(j["id"])
                        if fresh is not None:
                            ok, reason = _score_only(fresh, profile, point_bank_items, candidate_years)
                            if ok:
                                _judge_progress_update(judged=_judge_progress["judged"] + 1)
                            else:
                                _judge_progress_update(failed=_judge_progress["failed"] + [
                                    {"job_id": fresh["id"], "title": fresh["title"],
                                     "company": fresh["company"], "reason": reason}])
                    except Exception as e:
                        # One job's unexpected exception (DB hiccup, bad row, etc.) must
                        # never take the rest of the batch down with it -- real bug found
                        # live: a 429-triggered failure path that raised instead of
                        # returning, which killed this thread mid-batch and left
                        # "running": true stuck forever (nothing clears it until the next
                        # server restart), so every subsequent AI-judge click bounced off
                        # the one-at-a-time lock looking exactly like "AI-judge is
                        # broken" when the real batch had silently died minutes in.
                        print(f"[jobs] judge batch: unexpected error on job {j.get('id')}: {e}", file=sys.stderr)
                        _judge_progress_update(failed=_judge_progress["failed"] + [
                            {"job_id": j.get("id"), "title": "", "company": "", "reason": f"unexpected error: {e}"}])
                    _judge_progress_update(done=_judge_progress["done"] + 1)
            finally:
                _judge_progress_update(running=False)
    threading.Thread(target=run, daemon=True).start()
    return True


# Generate, async -- same pattern as the judge batch above. The real
# automated path: Zai drafts the summary + cover letter
# (_zai_draft_application, grounded in the real point bank, gated by the
# same humanizer check generate() itself enforces), then the existing
# generate_cli.py pipeline renders it exactly like a Claude-authored draft
# would -- same one-page enforcement, same Keyword+Use+Result bullet
# filter, same PDF export. One quality bar regardless of who wrote the
# prose.
_generate_lock = threading.Lock()
_generate_progress = {"running": False, "total": 0, "done": 0, "generated": 0,
                       "failed": [], "started_ts": None}


def _generate_one(job_id, summary, cover_letter_body, selection):
    """Shells into this project's own .venv the same way the manual
    /api/jobs/generate handler does -- one real code path for "how a
    resume gets rendered" regardless of whether Claude or Zai wrote the
    prose. `selection` is _zai_generate_and_score's output, passed through
    as plain data -- generate.py never calls an LLM itself.
    Returns (ok, error_or_None)."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"job_id": job_id, "summary": summary, "cover_letter_body": cover_letter_body,
                    "selection": selection}, f)
        req_path = f.name
    try:
        out = subprocess.run(
            # Was 60s -- too tight on a slower VPS box. The one-page trim loop can call
            # soffice (docx->pdf) several times in one generate, and a cold/resource-
            # constrained LibreOffice invocation is slower than on a dev machine. Found
            # live: a real VPS generate got killed mid-render, leaving resume.pdf on disk
            # but no cover letter, ats_estimate.md, or validation_checklist.md -- the
            # subprocess was killed by this timeout before generate.generate() finished.
            [VENV_PY, os.path.join(ROOT, "generate_cli.py"), req_path],
            capture_output=True, text=True, timeout=240, cwd=ROOT,
        )
    finally:
        os.unlink(req_path)
    if out.returncode != 0:
        return False, (out.stderr or out.stdout)[-500:]
    try:
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return False, f"bad generate_cli output: {out.stdout[-300:]} {out.stderr[-300:]}"
    if "error" in result:
        return False, result["error"][:500]
    return True, None


def _generate_single_job(jid):
    """The same score->select->draft->render pipeline _run_generate_batch
    runs per job in a batch, but for exactly one job, synchronous, no
    forced project -- what the poll script's emailed "Generate now" link
    triggers. Returns (ok, error_or_None)."""
    job = jobsdb.jobs_get(jid)
    if not job:
        return False, "no such job"
    profile, skill_names, _bullets_text, _skills_line, candidate_years = _jobs_resume_body()
    point_bank_items = jobsdb.point_bank_all()
    selection = _zai_generate_and_score(job, profile, point_bank_items, skill_names,
                                         candidate_years, tuning_key="generate_single")
    if selection is None:
        return False, "content selection/scoring call failed -- see server logs"
    points_text = _points_text_for_selection(point_bank_items, selection["priority_items"])
    summary, cover, draft_err = _zai_draft_application(job, profile, points_text, skill_names,
                                                         candidate_years, tuning_key="generate_single")
    if summary is None:
        return False, draft_err
    ok, err = _generate_one(jid, summary, cover, selection)
    if not ok:
        return False, err
    jobsdb.jobs_set_llm_score(jid, selection["score"], selection["reason"],
                               missing=selection["missing"], projects=selection["project_assessment"],
                               requirements=selection["requirements"])
    return True, None


def _run_generate_batch(candidates, profile, point_bank_items, skill_names, candidate_years):
    """candidates: queued jobs. Returns False without starting anything if
    a batch is already running -- same one-at-a-time rule the judge batch
    uses, so two clicks don't double-draft the same jobs.

    point_bank_items: jobsdb.point_bank_all() rows (for
    _zai_generate_and_score, which needs real ids/entry_type).
    _zai_generate_and_score runs BEFORE _zai_draft_application (the cover
    letter) -- the cover letter is scoped via _points_text_for_selection to
    only the items actually selected for the resume, instead of seeing the
    entire point bank and risking a fact the resume itself doesn't include."""
    if _generate_progress["running"]:
        return False
    _generate_progress.update({"running": True, "total": len(candidates), "done": 0,
                                "generated": 0, "failed": [], "started_ts": time.time()})

    def run():
        with _generate_lock:
            for j in candidates:
                fresh = jobsdb.jobs_get(j["id"])
                if fresh is not None and fresh["status"] == "queued":
                    selection = _zai_generate_and_score(
                        fresh, profile, point_bank_items, skill_names, candidate_years)
                    if selection is None:
                        print(f"[jobs] generate-queued: selection call failed for job {fresh['id']}",
                              file=sys.stderr)
                        _generate_progress["failed"].append(
                            {"job_id": fresh["id"], "title": fresh["title"], "company": fresh["company"],
                             "reason": "content selection/scoring call failed -- see server logs"})
                        _generate_progress["done"] += 1
                        continue
                    points_text = _points_text_for_selection(point_bank_items, selection["priority_items"])
                    summary, cover, draft_err = _zai_draft_application(
                        fresh, profile, points_text, skill_names, candidate_years)
                    if summary is None:
                        print(f"[jobs] generate-queued: draft failed for job {fresh['id']}: {draft_err}",
                              file=sys.stderr)
                        _generate_progress["failed"].append(
                            {"job_id": fresh["id"], "title": fresh["title"],
                             "company": fresh["company"], "reason": draft_err})
                        _generate_progress["done"] += 1
                        continue
                    ok, err = _generate_one(fresh["id"], summary, cover, selection)
                    if ok:
                        _generate_progress["generated"] += 1
                        jobsdb.jobs_set_llm_score(fresh["id"], selection["score"], selection["reason"],
                                                   missing=selection["missing"],
                                                   projects=selection["project_assessment"],
                                                   requirements=selection["requirements"])
                    else:
                        print(f"[jobs] generate-queued: render failed for job {fresh['id']}: {err}",
                              file=sys.stderr)
                        _generate_progress["failed"].append(
                            {"job_id": fresh["id"], "title": fresh["title"],
                             "company": fresh["company"], "reason": err})
                _generate_progress["done"] += 1
            _generate_progress["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


# Fetch, async -- same background-thread-plus-polling pattern as the
# generate batch above. The UI polls /api/jobs/refetch/status the same way
# it already polls generate-queued.
_fetch_lock = threading.Lock()
_fetch_progress = {"running": False, "started_ts": None, "ok": None, "new_postings": None,
                    "added_today": None, "log": ""}


def _run_fetch_async():
    """Returns False without starting anything if a fetch is already
    running -- one at a time, same "no double-spend / no double-run"
    reasoning as the judge batch."""
    if _fetch_progress["running"]:
        return False
    _fetch_progress.update({"running": True, "started_ts": time.time(), "ok": None,
                             "new_postings": None, "log": ""})

    def run():
        with _fetch_lock:
            try:
                before = {j["id"] for j in jobsdb.jobs_list()}
                out = subprocess.run([VENV_PY, "fetch_ats.py"], capture_output=True,
                                      text=True, timeout=600, cwd=ROOT)
                after_jobs = jobsdb.jobs_list()
                new_job_ids = {j["id"] for j in after_jobs if j["id"] not in before}
                _fetch_progress.update({
                    "ok": out.returncode == 0, "new_postings": len(new_job_ids),
                    "added_today": jobsdb.jobs_added_count(_day_start_ts(0)),
                    "log": out.stdout[-4000:] + out.stderr[-2000:], "running": False,
                })
            except subprocess.TimeoutExpired as e:
                partial = (e.stdout or b"").decode(errors="replace")[-4000:]
                _fetch_progress.update({
                    "ok": False, "new_postings": 0,
                    "added_today": jobsdb.jobs_added_count(_day_start_ts(0)),
                    "log": f"Timed out after 600s -- partial output:\n{partial}", "running": False,
                })
            except Exception as e:
                _fetch_progress.update({"ok": False, "new_postings": 0,
                                         "added_today": jobsdb.jobs_added_count(_day_start_ts(0)),
                                         "log": f"Fetch crashed: {e}", "running": False})
    threading.Thread(target=run, daemon=True).start()
    return True


def _extract_file_text(path, ext):
    """PDF via pdftotext (system-wide), docx via this project's own venv
    (python-docx isn't installed for this process's plain python3).
    Returns "" on failure rather than raising -- an unreadable upload
    should surface as "found nothing", not crash the request."""
    try:
        if ext == ".pdf":
            out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=30)
            return out.stdout
        if ext == ".docx":
            code = ("import sys\nfrom docx import Document\n"
                    "d = Document(sys.argv[1])\n"
                    "print('\\n'.join(p.text for p in d.paragraphs))")
            out = subprocess.run([VENV_PY, "-c", code, path],
                                  capture_output=True, text=True, timeout=30)
            return out.stdout
        if ext in (".txt", ".md"):
            with open(path, "r", errors="replace") as f:
                return f.read()
    except Exception as e:
        print(f"[profile] extract failed: {e}", file=sys.stderr)
    return ""


def _zai_extract_profile(resume_text, known_companies, existing_points_text):
    """Structured extraction: real resume/pasted text in, candidate
    point_bank rows + skills out -- grounded, never invents beyond what's
    literally in the text. Caller treats the result as a PROPOSAL for human
    review, never auto-committed, same discipline as everywhere else in
    this project.

    Also applies the same Keyword+Use+Result reshaping _zai_rewrite_point
    does for a single manually-typed bullet, in this SAME call instead of a
    second pass per point. A point with no real result anywhere in the
    source text is NOT invented a number -- it's extracted as written and
    flagged needs_review with what's missing.

    existing_points_text is shown to the model so it can skip proposing a
    point that just reworded something already on file -- resume variants
    kept re-extracting the same accomplishment with slightly different
    phrasing and it was landing in the point bank twice with no check at
    all. point_bank_find_similar (difflib, in db.py) is the deterministic
    safety net for near-verbatim repeats specifically."""
    system = (
        'Extract structured career facts from a resume or pasted text, and shape each one into a strong '
        'resume bullet. Rules: (1) never invent a fact, number, skill, or result not literally stated in '
        'the text -- if something is ambiguous, leave it out rather than guess. (2) One point_bank row per '
        'distinct accomplishment. (3) For each accomplishment, if the source text already contains a real '
        'result (a number, a named outcome, an award, concrete feedback) but states it plainly or buries '
        'it, reshape it into: the real skill/technology, how it was actually used, and that measurable '
        'result -- one bullet, one or two sentences, preserving every real fact and number exactly as '
        'given. No AI-sounding buzzwords (delve, leverage, underscore, boast, robust, seamless), no em '
        'dashes. (4) If an accomplishment has NO real result or outcome anywhere in the source text -- a '
        'bare activity or responsibility with nothing measurable or named -- do NOT invent one. Extract it '
        'as written (lightly cleaned up only), set needs_review=true, and give a one-sentence review_note '
        'explaining what\'s missing plus one specific question that would help recall the real fact. '
        '(5) Reuse company names exactly as they already appear in the candidate\'s known companies list '
        'if the resume clearly refers to the same employer. (6) Do NOT propose a point that describes the '
        'same underlying accomplishment as one already listed below, even if the wording differs -- skip '
        'it entirely rather than re-adding a reworded duplicate. Only propose points for facts/numbers not '
        'already covered.\n'
        'Reply with ONLY a JSON object: {"points": [{"company": str, "role": str, "start_date": '
        '"YYYY-MM or empty", "end_date": "YYYY-MM or empty (empty means current)", "text": str, '
        '"tags": [str, ...], "needs_review": bool, "review_note": "str or empty"}], '
        '"skills": [{"name": str, "category": str}]}.'
    )
    user = (f"Known companies already on file: {', '.join(known_companies) or '(none yet)'}\n\n"
            f"Accomplishments already in the point bank -- do not re-propose any of these, reworded or not:\n"
            f"{existing_points_text or '(none yet)'}\n\n"
            f"Resume text:\n{_trim_jd(resume_text, limit=15000)}")
    reply, provider = _llm_json_call(system, [{"role": "user", "content": user}], max_tokens=32000, temperature=0.1,
                                      **_llm_tuning("extract_profile"))
    if reply is None:
        return None, "no LLM provider configured, or both Zai and Claude calls failed -- check server logs"
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        return obj, None
    except Exception as e:
        return None, f"couldn't parse the model's response: {e}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _html(self, body_text, code=200):
        body = body_text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path, root=STATIC):
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(root) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = {".html": "text/html", ".js": "application/javascript",
                 ".css": "text/css", ".json": "application/json"}.get(
            os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_application_file(self, rel):
        """Download a generated resume.docx / cover_letter.docx from
        data/applications/. Same normpath-containment check as _static,
        since rel comes straight from the URL."""
        full = os.path.normpath(os.path.join(APPLICATIONS, rel))
        if not full.startswith(APPLICATIONS) or not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".pdf": "application/pdf",
            ".md": "text/markdown",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(full)}"')
        self.end_headers()
        self.wfile.write(body)

    # -- routing
    def do_GET(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)

        if p == "/auth/google/login":
            # Starts the login flow: a random, single-use state value goes
            # both into a short-lived cookie and into the URL Google will
            # echo back on the callback -- the callback refuses to proceed
            # unless they match, which is what stops a forged callback
            # request from logging an attacker's code in as a session.
            state = secrets.token_urlsafe(24)
            self.send_response(302)
            self.send_header("Location", _google_auth_redirect_url(state))
            self.send_header("Set-Cookie", f"oauth_state={state}; Max-Age=600; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if p == "/auth/google/callback":
            code = qs.get("code", [""])[0]
            state = qs.get("state", [""])[0]
            cookie_state = _parse_cookies(self.headers.get("Cookie", "")).get("oauth_state")
            if not code or not state or not cookie_state or state != cookie_state:
                return self._html("<h1>Login failed</h1><p>Invalid or expired login attempt -- "
                                   "try <a href=\"/auth/google/login\">logging in</a> again.</p>", 400)
            email = _google_exchange_code(code)
            if not email or email != ALLOWED_LOGIN_EMAIL:
                return self._html("<h1>Not authorized</h1><p>That Google account isn't allowed to "
                                   "access this app.</p>", 403)
            self.send_response(302)
            self.send_header("Location", "/jobs/")
            self.send_header("Set-Cookie", f"session={_make_session_cookie(email)}; "
                              f"Max-Age={SESSION_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not p.startswith(AUTH_EXEMPT_PREFIXES) and not _valid_session_email(self.headers.get("Cookie", "")):
            self.send_response(302)
            self.send_header("Location", "/auth/google/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if p == "/api/jobs/generate-link":
            # The one-click "Generate now" link Telegram-notified by
            # poll_and_notify.py. Deliberately exempted above from the
            # Google OAuth gate -- opened fresh from a Telegram message,
            # not a live logged-in browser session. The HMAC signature IS
            # the auth here: only a link this server itself signed (via
            # _sign_job_id, using a secret that never leaves the server)
            # can trigger a real generate. A plain HTML response, not
            # JSON -- a human opens this link in a browser, not an API
            # client.
            try:
                jid = int(qs.get("job_id", ["0"])[0])
            except ValueError:
                jid = 0
            sig = qs.get("sig", [""])[0]
            if not jid or not _verify_job_sig(jid, sig):
                return self._html("<h1>Invalid or expired link</h1>", 403)
            ok, err = _generate_single_job(jid)
            if ok:
                return self._html(
                    f"<h1>Generated</h1><p>Resume and cover letter are ready -- "
                    f"<a href=\"/jobs/\">open the app</a> to download them.</p>")
            # Log the real reason regardless of whether the client is still connected --
            # found live: a slow generate (large JD, several retries) outlasted the
            # browser's own patience, so by the time this tried to WRITE the error back,
            # the client had already disconnected (BrokenPipeError), and the actual
            # reason was never visible anywhere except inside that unsent HTML body.
            print(f"[jobs] generate-link failed for job {jid}: {err}", file=sys.stderr)
            return self._html(f"<h1>Generation failed</h1><p>{err}</p>", 500)

        if p == "/api/jobs/view-details":
            # The "View JD" button in the Telegram notification -- kept the
            # full job description and fit reasoning OUT of the message
            # itself (that's what was making every notification huge), one
            # tap away instead. Same HMAC-signature auth as generate-link,
            # same reason it's exempt from the login gate.
            try:
                jid = int(qs.get("job_id", ["0"])[0])
            except ValueError:
                jid = 0
            sig = qs.get("sig", [""])[0]
            if not jid or not _verify_job_sig(jid, sig):
                return self._html("<h1>Invalid or expired link</h1>", 403)
            job = jobsdb.jobs_get(jid)
            if not job:
                return self._html("<h1>No such job</h1>", 404)
            import html as _html_lib
            score = job["llm_score"]
            score_line = f"<p><b>ATS score: {score:.0f}</b></p>" if score is not None else ""
            return self._html(
                f"<h1>{_html_lib.escape(job['title'])}</h1>"
                f"<p>{_html_lib.escape(job['company'])} &middot; "
                f"<a href=\"{_html_lib.escape(job.get('url', ''))}\">apply here</a></p>"
                f"{score_line}"
                f"<p>{_html_lib.escape(job.get('llm_reason', '') or '(not scored yet)')}</p>"
                f"<hr><pre style=\"white-space:pre-wrap;font-family:sans-serif\">"
                f"{_html_lib.escape(job.get('description_raw', ''))}</pre>")

        if p == "/api/jobs/list":
            total_unfiltered, matched = _filter_jobs(qs)
            page = max(1, int(qs.get("page", ["1"])[0]))
            per_page = max(1, min(100, int(qs.get("per_page", ["10"])[0])))
            applied_ids = {a["job_id"] for a in jobsdb.applications_all() if a["status"] != "generated"}
            start = (page - 1) * per_page
            page_jobs = [_job_public(j, applied_ids) for j in matched[start:start + per_page]]
            return self._json({"jobs": page_jobs, "total_matched": len(matched), "total_unfiltered": total_unfiltered,
                                "page": page, "per_page": per_page,
                                "total_pages": max(1, (len(matched) + per_page - 1) // per_page)})

        if p == "/api/jobs/companies":
            _, matched = _filter_jobs(qs)
            by_company = {}
            for j in matched:
                c = by_company.setdefault(j["company"], {"company": j["company"], "count": 0,
                                                           "best_score": None, "best_title": ""})
                c["count"] += 1
                eff = j["llm_score"]
                if eff is not None and (c["best_score"] is None or eff > c["best_score"]):
                    c["best_score"] = eff
                    c["best_title"] = j["title"]
            rows = sorted(by_company.values(),
                          key=lambda c: c["best_score"] if c["best_score"] is not None else -1, reverse=True)
            return self._json({"companies": rows})

        if p == "/api/jobs/missing-clusters":
            # Cached, not recomputed on every tab-open -- this used to cost
            # a real LLM call every single time the Gaps tab was clicked.
            # Plain tab-open (no refresh=1) returns the last computed
            # result instantly; only an explicit Refresh (or a scoped
            # rejudge finishing, see rejudge-cluster below) recomputes and
            # overwrites the cache.
            cached = jobsdb.gap_clusters_get()
            if qs.get("refresh", ["0"])[0] != "1" and cached is not None:
                return self._json(cached)
            scored = [j for j in jobsdb.jobs_list() if j["llm_score"] is not None]
            clusters = _cluster_missing_gaps(scored)
            result = {"clusters": clusters, "scored_count": len(scored), "computed_ts": time.time()}
            jobsdb.gap_clusters_set(result)
            return self._json(result)

        if p == "/api/jobs/llm-backfill/status":
            # Polled by the UI while a batch is running -- real progress
            # (judged so far / total), not a guess, since each score is
            # written to the db the moment that one call finishes.
            return self._json(dict(_judge_progress))

        if p == "/api/jobs/refetch/status":
            return self._json(dict(_fetch_progress))

        if p == "/api/jobs/generate-queued/status":
            return self._json(dict(_generate_progress))

        if p == "/api/jobs/detail":
            jid = int(qs.get("id", ["0"])[0])
            j = jobsdb.jobs_get(jid)
            if not j:
                return self._json({"error": "no such job"}, 404)
            j["stale"] = j["llm_score"] is not None and jobsdb.jobs_llm_score_stale(j)
            j["llm_missing"] = _parse_json_col(j["llm_missing"])
            j["llm_projects"] = _parse_json_col(j["llm_projects"])
            return self._json({"job": j, "applications": jobsdb.applications_for_job(jid)})

        if p == "/api/applications/list":
            return self._json({"applications": jobsdb.applications_all()})

        if p == "/api/profile/points":
            show_all = qs.get("all", ["0"])[0] == "1"
            points = jobsdb.point_bank_all(active_only=not show_all)
            # Keyword+Use+Result rubric, authoring-time half (brag + no bare
            # generic-verb opener) -- surfaced on every real point here so a
            # weak one is visible at a glance in Manage Entries. A
            # bracket-flagged draft row is skipped -- not real resume
            # content yet, nothing to grade.
            for pt in points:
                pt["quality"] = (None if pt["text"].lstrip().startswith("[")
                                  else jobs_ats.check_bullet_brag(pt["text"]))
            return self._json({"points": points})

        if p == "/api/profile/skills":
            return self._json({"skills": jobsdb.skills_all()})

        if p == "/api/profile/fields":
            return self._json({"profile": jobsdb.profile_all()})

        if p == "/":
            self.send_response(302)
            self.send_header("Location", "/jobs/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if p == "/jobs" or p.startswith("/jobs/"):
            return self._static(p[len("/jobs"):] or "/")

        if p.startswith("/applications/"):
            return self._serve_application_file(p[len("/applications/"):])

        return self._static(p)

    def _jobs_post(self, p, b):
        if p == "/api/jobs/manual":
            # No regex score computed on add -- queue it and run AI-judge
            # (on this posting or in a batch) to get a real fit score.
            company, title = b.get("company", "").strip(), b.get("title", "").strip()
            text, url = b.get("text", "").strip(), b.get("url", "").strip()
            location = b.get("location", "").strip()
            if not company or not title or not text:
                return self._json({"error": "company, title, and text are required"}, 400)
            jid, is_new = jobsdb.jobs_add("manual", company, title, url, text, location)
            return self._json({"job_id": jid, "is_new": is_new})

        if p == "/api/jobs/queue":
            jobsdb.jobs_set_status(int(b.get("job_id", 0)), "queued")
            return self._json({"ok": True})

        if p == "/api/jobs/mark-applied":
            # Direct "I already applied" for the common case that never
            # touches the generate pipeline at all -- applied on the
            # company's site or LinkedIn with your own resume.
            jid = int(b.get("job_id", 0))
            if not jobsdb.jobs_get(jid):
                return self._json({"error": "no such job"}, 404)
            aid = jobsdb.applications_quick_mark_applied(
                jid, b.get("via", "other"), b.get("referral_name", ""), b.get("referral_note", ""))
            return self._json({"ok": True, "application_id": aid})

        if p == "/api/jobs/refetch":
            # Fetch only -- no scoring of any kind happens here. The only
            # real pre-generation fit judgment is the LLM one, a separate,
            # explicit action (AI-judge / /api/jobs/llm-backfill) on
            # purpose -- it has a real cost per call. Async -- see
            # _run_fetch_async. Starts the background fetch and returns
            # immediately; the UI polls /api/jobs/refetch/status.
            started = _run_fetch_async()
            if not started:
                return self._json({"error": "A fetch is already running"}, 409)
            return self._json({"started": True})

        if p == "/api/jobs/llm-backfill":
            # The ONLY pre-generation LLM judgment -- fully manual, triggered
            # by this button, never automatically on fetch. Judges any real
            # engineering title (not manager-tier) in India/Bangalore/
            # Bengaluru that's either never been judged, OR was judged
            # before the master doc/skills/profile last changed
            # (jobs_llm_score_stale). Runs the whole eligible set, async --
            # no per-call cap, see _run_judge_batch. days: same plain
            # day-count as the list filter, defaults to 14 when not given
            # at all. The "Clear & re-judge" flow can still explicitly pass
            # days=0 to deliberately scrub everything, old postings
            # included.
            profile, _skill_names, _bullets_text, _skills_line, candidate_years = _jobs_resume_body()
            point_bank_items = jobsdb.point_bank_all()
            date_cutoff = _days_cutoff(b.get("days"), default=14)
            candidates = [j for j in jobsdb.jobs_list()
                          if jobs_ats.is_likely_engineering_title(j["title"])
                          and jobs_ats.title_seniority_tier(j["title"]) != "manager"
                          and jobsdb.jobs_llm_score_stale(j)
                          and jobs_ats.is_target_location(j["location"], j["description_raw"])
                          and (date_cutoff is None or j["fetched_ts"] >= date_cutoff)]
            started = _run_judge_batch(candidates, profile, point_bank_items, candidate_years)
            if not started:
                return self._json({"error": "AI-judge is already running"}, 409)
            return self._json({"started": True, "candidates": len(candidates)})

        if p == "/api/jobs/rescore":
            # Single-job version of AI-judge -- for when the master doc
            # changed and only one posting's score needs to catch up, not
            # a whole batch re-spend. Synchronous: one LLM call.
            jid = int(b.get("job_id", 0))
            job = jobsdb.jobs_get(jid)
            if not job:
                return self._json({"error": "no such job"}, 404)
            profile, _skill_names, _bullets_text, _skills_line, candidate_years = _jobs_resume_body()
            ok, reason = _score_only(job, profile, jobsdb.point_bank_all(), candidate_years,
                                      tuning_key="rescore_single")
            if not ok:
                return self._json({"error": reason}, 500)
            fresh = jobsdb.jobs_get(jid)
            fresh["llm_missing"] = _parse_json_col(fresh["llm_missing"])
            fresh["llm_projects"] = _parse_json_col(fresh["llm_projects"])
            return self._json({"job": fresh})

        if p == "/api/jobs/rejudge-cluster":
            # Scoped rejudge from the Gaps tab: exactly the postings a
            # specific gap cluster is actually sourced from -- re-run with
            # whatever the master doc looks like right now. Reuses
            # _run_judge_batch as-is.
            job_ids = [int(x) for x in b.get("job_ids", [])]
            if not job_ids:
                return self._json({"error": "no job_ids given"}, 400)
            profile, _skill_names, _bullets_text, _skills_line, candidate_years = _jobs_resume_body()
            point_bank_items = jobsdb.point_bank_all()
            candidates = [{"id": jid} for jid in job_ids]
            started = _run_judge_batch(candidates, profile, point_bank_items, candidate_years)
            if not started:
                return self._json({"error": "AI-judge is already running"}, 409)
            return self._json({"started": True, "candidates": len(candidates)})

        if p == "/api/jobs/generate-with-project":
            # "Add project to resume" -- a full regenerate (fresh summary +
            # cover letter, fresh content selection+score), except these
            # project ids are forced into the content regardless of their
            # own worth_including verdict. project_ids: every point-bank
            # row belonging to the one project being added (a project
            # with several facts on file is still ONE project -- forcing
            # only its first row would leave the rest out). Synchronous:
            # three real LLM calls (score, select content, draft prose)
            # plus rendering.
            jid = int(b.get("job_id", 0))
            project_ids = [int(x) for x in b.get("project_ids", [])]
            job = jobsdb.jobs_get(jid)
            if not job:
                return self._json({"error": "no such job"}, 404)
            profile, skill_names, _bullets_text, _skills_line, candidate_years = _jobs_resume_body()
            point_bank_items = jobsdb.point_bank_all()
            selection = _zai_generate_and_score(job, profile, point_bank_items, skill_names,
                                                 candidate_years, forced_project_ids=project_ids,
                                                 tuning_key="generate_single")
            if selection is None:
                print(f"[jobs] generate-with-project: selection call failed for job {jid}", file=sys.stderr)
                return self._json({"error": "content selection/scoring call failed -- see server logs"}, 500)
            # Scoped to only the items actually selected for the resume --
            # was the entire point bank, drafted BEFORE content selection
            # had even run, so the cover letter could reference a fact the
            # resume itself didn't include.
            points_text = _points_text_for_selection(point_bank_items, selection["priority_items"])
            summary, cover, draft_err = _zai_draft_application(job, profile, points_text, skill_names,
                                                                 candidate_years, tuning_key="generate_single")
            if summary is None:
                # Logged here, not just returned -- a slow request (several LLM retries)
                # can outlast the client's own patience, and the real reason was
                # otherwise only ever visible inside a JSON body nothing ever read.
                print(f"[jobs] generate-with-project: draft failed for job {jid}: {draft_err}", file=sys.stderr)
                return self._json({"error": draft_err}, 500)
            ok, err = _generate_one(jid, summary, cover, selection)
            if not ok:
                print(f"[jobs] generate-with-project: render failed for job {jid}: {err}", file=sys.stderr)
                return self._json({"error": err}, 500)
            jobsdb.jobs_set_llm_score(jid, selection["score"], selection["reason"],
                                       missing=selection["missing"], projects=selection["project_assessment"],
                                       requirements=selection["requirements"])
            return self._json({"ok": True})

        if p == "/api/jobs/clear-llm-scores":
            # Explicit "force reload" -- wipes AI scores so the next
            # AI-judge run re-checks them from scratch.
            date_cutoff = _days_cutoff(b.get("days"))
            n = jobsdb.jobs_clear_llm_scores(date_cutoff=date_cutoff)
            return self._json({"cleared": n})

        if p == "/api/jobs/generate-queued":
            # The real fix for "I queued it and nothing happened". Drafts +
            # renders every job currently status='queued', async (see
            # _run_generate_batch); each one costs three real Zai calls now
            # (score requirements, select+word the content, then draft the
            # summary/cover letter prose).
            profile, skill_names, _bullets_text, _skills_line, candidate_years = _jobs_resume_body()
            point_bank_items = jobsdb.point_bank_all()
            candidates = [j for j in jobsdb.jobs_list() if j["status"] == "queued"]
            started = _run_generate_batch(candidates, profile, point_bank_items,
                                           skill_names, candidate_years)
            if not started:
                return self._json({"error": "A generate batch is already running"}, 409)
            return self._json({"started": True, "candidates": len(candidates)})

        if p == "/api/jobs/generate":          # posted only by Claude, via curl -- the
            # resume/cover letter text is authored per job, never auto-templated.
            # selection (content choice + score) is likewise supplied by the
            # caller here -- this endpoint never calls an LLM itself.
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump({"job_id": b.get("job_id"), "summary": b.get("summary", ""),
                           "cover_letter_body": b.get("cover_letter_body", ""),
                           "selection": b.get("selection", {})}, f)
                req_path = f.name
            try:
                out = subprocess.run(
                    # Was 30s -- same VPS timeout fix as _generate_one's call below, see
                    # that comment for why.
                    [VENV_PY, os.path.join(ROOT, "generate_cli.py"), req_path],
                    capture_output=True, text=True, timeout=240, cwd=ROOT,
                )
            finally:
                os.unlink(req_path)
            if out.returncode != 0:
                return self._json({"error": (out.stderr or out.stdout)[-2000:]}, 500)
            try:
                return self._json(json.loads(out.stdout.strip().splitlines()[-1]))
            except Exception:
                return self._json({"error": f"bad generate_cli output: {out.stdout} {out.stderr}"}, 500)

        if p == "/api/applications/mark-applied":
            jobsdb.applications_mark_applied(int(b.get("app_id", 0)), b.get("via", "other"),
                                              referral_name=b.get("referral_name", ""),
                                              referral_note=b.get("referral_note", ""))
            return self._json({"ok": True})

        if p == "/api/applications/add-status":
            jobsdb.applications_add_status_event(int(b.get("app_id", 0)), b.get("status", ""),
                                                  note=b.get("note", ""))
            return self._json({"ok": True})

        if p == "/api/profile/points/rewrite-check":
            # Check-only -- never writes to the DB. Runs BEFORE a save so
            # the UI can show the full proposed rewrite (or exactly what's
            # missing) and let the user decide, rather than silently
            # flagging a bullet after the fact. Never invents a fact.
            text = (b.get("text") or "").strip()
            if not text:
                return self._json({"status": "error", "message": "no text given"})
            result = _zai_rewrite_point(text, b.get("company", ""), b.get("role", ""))
            if result is None:
                return self._json({"status": "error",
                                    "message": "Zai call failed -- couldn't check this bullet right now. "
                                                "You can retry, or save as written."})
            return self._json(result)

        if p == "/api/profile/points/tidy":
            # The "Tidy master doc" button -- proposal only, never writes.
            # Scans every company at once, returns only the ones that
            # actually have something to combine.
            def _tidy_one(company, role):
                existing = [pt for pt in jobsdb.point_bank_all(active_only=True)
                            if pt["company"] == company and not pt["text"].lstrip().startswith("[")]
                existing_texts = [pt["text"] for pt in existing]
                result = _zai_tidy_company(company, role, existing_texts)
                if result is None:
                    return {"company": company, "role": role, "unchanged": True,
                            "current_texts": existing_texts, "proposed_texts": existing_texts, "notes": [],
                            "error": "Zai call failed -- couldn't check, nothing changed."}
                unchanged = sorted(result["points"]) == sorted(existing_texts)
                return {"company": company, "role": role, "unchanged": unchanged,
                        "current_texts": existing_texts, "proposed_texts": result["points"],
                        "notes": result["notes"]}

            all_points = [pt for pt in jobsdb.point_bank_all(active_only=True)
                          if not pt["text"].lstrip().startswith("[")]
            by_company = {}
            for pt in all_points:
                by_company.setdefault(pt["company"], pt["role"])
            results = [r for co, role in by_company.items() for r in [_tidy_one(co, role)] if not r["unchanged"]]
            return self._json({"companies": results, "checked_points": len(all_points)})

        if p == "/api/profile/points/update":
            quality = jobs_ats.check_bullet_brag(b.get("text", ""))
            jobsdb.point_bank_update_text(int(b.get("id", 0)), b.get("text", ""))
            return self._json({"ok": True, "quality_warning": None if quality["ok"] else quality})

        if p == "/api/profile/points/toggle":
            jobsdb.point_bank_set_active(int(b.get("id", 0)), bool(b.get("active", True)))
            return self._json({"ok": True})

        if p == "/api/profile/points/tidy-commit":
            # Applies an accepted tidy result for one or more companies.
            # The model only ever gave us text in/text out -- this is the
            # plain diff that decides what actually happens on disk:
            #   - an existing point whose text survives UNCHANGED in the
            #     accepted list -> left completely alone, same row/id.
            #   - an existing point whose text does NOT survive -> it was
            #     folded into a merge, retire it (point_bank_set_active --
            #     never destructively deleted).
            #   - any accepted text that doesn't match an existing point ->
            #     genuinely new (a merge result, or the new point from an
            #     add), inserted fresh.
            companies = b.get("companies", [])
            updated = 0
            for entry in companies:
                company = entry.get("company", "")
                role = entry.get("role", "")
                accepted_texts = [str(t).strip() for t in entry.get("texts", []) if str(t).strip()]
                existing = [pt for pt in jobsdb.point_bank_all(active_only=True) if pt["company"] == company]
                existing_by_text = {}
                for pt in existing:
                    existing_by_text.setdefault(pt["text"], pt)
                accepted_set = set(accepted_texts)
                retired = [pt for text, pt in existing_by_text.items() if text not in accepted_set]
                new_texts = [t for t in accepted_texts if t not in existing_by_text]
                if not retired and not new_texts:
                    continue   # this company genuinely didn't change -- nothing to do
                starts = [r["start_date"] for r in retired if r["start_date"]]
                ends = [r["end_date"] for r in retired]
                tags = sorted({t for r in retired for t in r["tags"]})
                entry_type = retired[0]["entry_type"] if retired else (existing[0]["entry_type"] if existing else "role")
                start = min(starts) if starts else ""
                end = None if (not ends or any(not e for e in ends)) else max(ends)
                for text in new_texts:
                    jobsdb.point_bank_add(company, role, start, end, text, tags=tags, entry_type=entry_type,
                                           source_note="tidied/merged via Manage entries")
                for pt in retired:
                    jobsdb.point_bank_set_active(pt["id"], False)
                updated += 1
            return self._json({"companies_updated": updated})

        if p == "/api/profile/skills/add":
            jobsdb.skills_add(b.get("name", ""), b.get("category", "Other"))
            return self._json({"ok": True})

        if p == "/api/profile/fields/set":
            jobsdb.profile_set(b.get("key", ""), b.get("value", ""))
            return self._json({"ok": True})

        if p == "/api/profile/extract":
            return self._profile_extract(b)

        if p == "/api/profile/extract/commit":
            return self._profile_extract_commit(b)

        self.send_error(404)

    def _profile_extract(self, b):
        """Upload OR pasted text -> extract -> propose. Never writes to the
        DB -- returns the proposal for the UI to show as an editable review
        list, each point marked with whether it looks like a near-duplicate
        of something already on file."""
        pasted_text = (b.get("text") or "").strip()
        if pasted_text:
            text = pasted_text
        else:
            filename = b.get("filename", "upload")
            content_b64 = b.get("content_base64", "")
            ext = os.path.splitext(filename)[1].lower()
            if ext not in (".pdf", ".docx", ".txt", ".md"):
                return self._json({"error": f"unsupported file type {ext or '(none)'} -- use .pdf, .docx, .txt or .md"}, 400)
            try:
                raw = base64.b64decode(content_b64)
            except Exception:
                return self._json({"error": "bad file upload"}, 400)

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(raw)
                tmp_path = f.name
            try:
                text = _extract_file_text(tmp_path, ext)
            finally:
                os.unlink(tmp_path)

        if not text.strip():
            return self._json({"error": "no text found -- paste some text or choose a different file"}, 400)

        existing = jobsdb.point_bank_all(active_only=False)
        known_companies = sorted({p["company"] for p in existing})
        existing_points_text = "\n".join(f"- [{p['company']}] {p['text']}" for p in existing)
        proposal, err = _zai_extract_profile(text, known_companies, existing_points_text)
        if err:
            return self._json({"error": err}, 500)

        # Deterministic safety net on top of the LLM's own dedup instinct --
        # difflib against what's actually on file, not just what the model
        # noticed. Marks, doesn't drop: the user still sees and decides.
        for pt in proposal.get("points", []):
            match, ratio = jobsdb.point_bank_find_similar(pt.get("text", ""))
            pt["likely_duplicate"] = match is not None
            pt["duplicate_of_text"] = match["text"] if match else None
        return self._json({"proposal": proposal})

    def _profile_extract_commit(self, b):
        """Writes exactly the points/skills the UI sends back -- after the
        user has reviewed, edited, and deselected whatever they didn't want.
        Still re-checks each point against the point bank right before
        insert (skip if a near-duplicate now exists)."""
        points = b.get("points", [])
        skills = b.get("skills", [])
        added_points, skipped_dupes, added_skills = 0, 0, 0
        for p in points:
            match, _ = jobsdb.point_bank_find_similar(p.get("text", ""))
            if match:
                skipped_dupes += 1
                continue
            tags = p.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            jobsdb.point_bank_add(p.get("company", ""), p.get("role", ""), p.get("start_date", ""),
                                   p.get("end_date") or None, p.get("text", ""), tags=tags,
                                   source_note="uploaded + extracted via Profile UI")
            added_points += 1
        for s in skills:
            jobsdb.skills_add(s.get("name", ""), s.get("category", "Other"))
            added_skills += 1
        return self._json({"added_points": added_points, "skipped_dupes": skipped_dupes,
                            "added_skills": added_skills})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path

        if not p.startswith(AUTH_EXEMPT_PREFIXES) and not _valid_session_email(self.headers.get("Cookie", "")):
            return self._json({"error": "not logged in"}, 401)

        b = self._body()

        if p.startswith("/api/jobs/") or p.startswith("/api/applications/") or p.startswith("/api/profile/"):
            return self._jobs_post(p, b)

        self.send_error(404)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"jobsearch  ->  http://{HOST}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
