"""Signed download links for the Telegram notification. They open from a chat
with no login session, so the signature is the credential: it covers the user,
the job and an expiry, so it can't be replayed for another user's files and
stops working after DAYS."""

import hashlib
import hmac
import os
import time

SECRET = os.environ.get("GENERATE_LINK_SECRET", "") or os.environ.get("ZAI_API_KEY", "")
DAYS = 7


def _sig(user_id, job_id, kind, exp):
    return hmac.new(SECRET.encode(), f"{user_id}|{job_id}|{kind}|{exp}".encode(), hashlib.sha256).hexdigest()


def download_url(base_url, user_id, job_id, kind):
    """kind: 'resume' or 'cover'."""
    exp = int(time.time()) + DAYS * 86400
    return (f"{base_url}/api/jobs/download?user={user_id}&job_id={job_id}&kind={kind}"
            f"&exp={exp}&sig={_sig(user_id, job_id, kind, exp)}")


def verify(user_id, job_id, kind, exp, sig):
    if not SECRET:
        return False
    try:
        if int(exp) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(_sig(user_id, job_id, kind, exp), sig or "")
