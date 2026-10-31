#!/usr/bin/env python3
"""Sends the "your resume and cover letter are ready" notification via
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


def _call(method, payload, bot_token=None):
    url = _API.format(token=bot_token or TELEGRAM_BOT_TOKEN, method=method)
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


def send_generation_complete(job, resume_url, cover_url, bot_token=None, chat_id=None):
    """The resume and cover letter for one job are ready. Both URLs are signed,
    expiring download links (links.py), not a login-gated page, so they open
    directly from this chat. bot_token/chat_id: this user's own Telegram
    config, falling back to the env-var admin bot when omitted."""
    bot_token = bot_token or TELEGRAM_BOT_TOKEN
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        return False, "no Telegram bot_token/chat_id for this recipient"
    text = f"*Generated*: {job['title']} @ {job['company']}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "Download resume", "url": resume_url},
                 {"text": "Download cover letter", "url": cover_url}],
            ]
        },
    }
    result, err = _call("sendMessage", payload, bot_token=bot_token)
    if err:
        return False, err
    if not result.get("ok"):
        return False, f"Telegram rejected the message: {result}"
    return True, None


def send_generation_failed(job, reason, bot_token=None, chat_id=None):
    """A job used up its attempts and failed -- this has to reach the person,
    not sit in a log nobody is watching."""
    bot_token = bot_token or TELEGRAM_BOT_TOKEN
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not bot_token or not chat_id:
        return False, "no Telegram bot_token/chat_id for this recipient"
    text = f"*Generation failed*: {job['title']} @ {job['company']}\n\n{(reason or '')[:300]}"
    result, err = _call("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                         bot_token=bot_token)
    if err:
        return False, err
    if not result.get("ok"):
        return False, f"Telegram rejected the message: {result}"
    return True, None


def send_test_message(bot_token, chat_id):
    """For the Profile tab's "Send test message" button -- proves a
    user's own saved bot_token/chat_id actually work, independent of the
    env-var admin bot the other send_* functions here use. Returns
    (ok, error_or_None), same convention as the rest of this file."""
    if not bot_token or not chat_id:
        return False, "bot token and chat id are both required"
    result, err = _call(
        "sendMessage",
        {"chat_id": chat_id, "text": "Test message from jobsearch -- your Telegram setup works."},
        bot_token=bot_token,
    )
    if err:
        return False, err
    if not result.get("ok"):
        return False, f"Telegram rejected the message: {result}"
    return True, None


def _print_chat_id():
    """Run once, by hand, after messaging your bot -- prints the chat id
    to put in TELEGRAM_CHAT_ID."""
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
