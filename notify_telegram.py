#!/usr/bin/env python3
"""Sends the "a job crossed your fit-score threshold" notification via
Telegram instead of email -- a bot token authenticates as a separate bot
identity, not your real account, so a leaked/misused token can't send as
you to your real contacts the way an email password could.

Setup: message @BotFather on Telegram, /newbot, get a token. Message your
new bot once (anything) so it has an open chat with you -- needed before
it can send you anything. Then TELEGRAM_CHAT_ID is your numeric chat id
with that bot, found via _print_chat_id() below.

    TELEGRAM_BOT_TOKEN=... python3 -c "import notify_telegram as n; n._print_chat_id()"

No dependency beyond the stdlib -- Telegram's Bot API is plain HTTPS/JSON.
"""
import json
import os
import urllib.error
import urllib.request

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_API = "https://api.telegram.org/bot{token}/{method}"


def _call(method, payload):
    url = _API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        return None, f"Telegram API {e.code}: {e.read().decode(errors='replace')[:300]}"
    except Exception as e:
        return None, str(e)


def send_job_alert(job, generate_url, view_url):
    """job: a real jobsdb row (needs title, company, url, llm_score).
    generate_url/view_url: the two signed one-click links. Returns
    (ok, error_or_None) -- never raises, so one bad send doesn't take the
    whole poll run down.

    Deliberately short: title, company, ATS score, apply link -- the full
    fit-reasoning and job description used to be inlined here and made
    every message huge; that's now one tap away via view_url instead."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- see notify_telegram.py docstring"

    text = (
        f"*ATS score: {job['llm_score']:.0f}*\n"
        f"{job['title']} @ {job['company']}\n\n"
        f"[Apply here]({job.get('url', '')})"
    )
    # Two inline buttons -- tap to act, no typing, nothing revealed until
    # you choose to open it. Both URLs ARE the auth (HMAC-signed, see
    # server.py's _sign_job_id) -- Telegram never sees or needs a
    # credential for either.
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "View JD", "url": view_url},
                 {"text": "Generate resume + cover letter", "url": generate_url}],
            ]
        },
    }
    result, err = _call("sendMessage", payload)
    if err:
        return False, err
    if not result.get("ok"):
        return False, f"Telegram rejected the message: {result}"
    return True, None


def send_generation_complete(job, resume_url, cover_url):
    """Follow-up message once an async "Generate now" click actually finishes
    -- the real fix for a generate that used to hang the original click for
    1-3+ minutes. Both URLs are HMAC-signed single-file download links (see
    server.py's _serve_signed_job_file), not a login-gated app page, so they
    open directly for whoever's on the other end of this chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"
    text = f"*Generated*: {job['title']} @ {job['company']}"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "Download resume", "url": resume_url},
                 {"text": "Download cover letter", "url": cover_url}],
            ]
        },
    }
    result, err = _call("sendMessage", payload)
    if err:
        return False, err
    if not result.get("ok"):
        return False, f"Telegram rejected the message: {result}"
    return True, None


def send_generation_failed(job, reason):
    """The other half of send_generation_complete -- an async generate can
    fail same as a synchronous one always could, and that needs to actually
    reach the person who clicked, not just sit in server logs nobody's
    watching in the moment."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"
    text = f"*Generation failed*: {job['title']} @ {job['company']}\n\n{(reason or '')[:300]}"
    result, err = _call("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})
    if err:
        return False, err
    if not result.get("ok"):
        return False, f"Telegram rejected the message: {result}"
    return True, None


def _print_chat_id():
    """Run once, by hand, after messaging your bot -- prints the chat id
    to put in TELEGRAM_CHAT_ID. Not called by poll_and_notify.py."""
    result, err = _call("getUpdates", {})
    if err:
        print(f"error: {err}")
        return
    updates = result.get("result", [])
    if not updates:
        print("No messages yet -- message your bot something first, then run this again.")
        return
    for u in updates[-5:]:
        msg = u.get("message", {})
        chat = msg.get("chat", {})
        print(f"chat_id={chat.get('id')}  from={chat.get('first_name', '?')}  text={msg.get('text', '')!r}")


if __name__ == "__main__":
    _print_chat_id()
