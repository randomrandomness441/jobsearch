"""Login: Google confirms WHO someone is, a signed cookie remembers it. Whether
that person may actually use the app is the users table (db.users_get) -- a
confirmed login that isn't in it is a guest."""

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from urllib.parse import urlencode

import links

PORT = int(os.environ.get("PORT", "8777"))
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", f"http://localhost:{PORT}/auth/google/callback")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "") or links.SECRET
SESSION_MAX_AGE = 30 * 24 * 3600
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def check_config():
    """Refuse to run with an empty signing secret: an HMAC keyed with "" is
    one anyone can compute, which would let them forge an admin cookie."""
    if not SESSION_SECRET:
        sys.exit("Set SESSION_SECRET (or GENERATE_LINK_SECRET) before starting the server.")


def _sign(payload):
    return hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(email):
    payload = f"{email}|{int(time.time()) + SESSION_MAX_AGE}"
    return f"{payload}|{_sign(payload)}"


def parse_cookies(cookie_header):
    cookies = {}
    for part in (cookie_header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies


def valid_session_email(cookie_header):
    """The logged-in email if the session cookie is present, correctly signed
    and unexpired, else None. A forged or expired cookie is treated exactly
    like no cookie."""
    raw = parse_cookies(cookie_header).get("session")
    if not raw or not SESSION_SECRET:
        return None
    try:
        email, expiry, sig = raw.split("|", 2)
        expired = int(expiry) < time.time()
    except ValueError:
        return None
    if expired or not hmac.compare_digest(_sign(f"{email}|{expiry}"), sig):
        return None
    return email


def google_auth_redirect_url(state):
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": GOOGLE_OAUTH_CLIENT_ID, "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code", "scope": "openid email profile", "state": state, "prompt": "select_account"})


def google_exchange_code(code):
    """Trades the auth code for the logged-in email (server to server -- the one
    place the client secret is used). None on any failure, or if Google hasn't
    verified the address: that is a failed login, never a free pass."""
    data = urlencode({"code": code, "client_id": GOOGLE_OAUTH_CLIENT_ID, "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                      "redirect_uri": OAUTH_REDIRECT_URI, "grant_type": "authorization_code"}).encode()
    try:
        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST",
                                     headers={"content-type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read()).get("access_token")
        if not token:
            return None
        req = urllib.request.Request(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read())
    except Exception as e:
        print(f"[auth] Google login failed: {e}", file=sys.stderr)
        return None
    return info.get("email") if info.get("email_verified") else None
