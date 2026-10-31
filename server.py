#!/usr/bin/env python3
"""The HTTP face of the app: routes in, JSON out. All the work happens
elsewhere -- worker.py (queue + fetch), pipeline.py (one job), db.py.

    ./server.py        then open http://localhost:8777
"""

import json
import os
import secrets
import sqlite3
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ats
import auth
import db
import gaps
import links
import master
import notify_telegram
import pipeline
import worker

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = auth.PORT

# Reachable without a session: the login flow itself, and the download route,
# which checks its own signed link (a Telegram message has no login).
AUTH_EXEMPT_PREFIXES = ("/auth/google/", "/api/jobs/download")

LEVEL_TIERS = {
    "core": ["mid", "senior"],
    "stretch": ["mid", "senior", "staff", "senior_staff"],
    "all": ["mid", "senior", "staff", "senior_staff", "manager"],
}
# download kind -> file name inside the job's folder
DOWNLOAD_FILES = {"resume": "resume.pdf", "cover": "cover_letter.pdf", "resume_docx": "resume.docx",
                  "cover_docx": "cover_letter.docx", "jd": "job_description.md"}
CONTENT_TYPES = {".pdf": "application/pdf", ".md": "text/markdown",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MAX_TITLE_LEN = 200


class BadRequest(Exception):
    pass


def _int(value, name="id"):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number")


def _days_cutoff(days):
    """first_seen_ts cutoff for 'fetched within N days'; blank or 0 = any time."""
    try:
        n = int(days)
    except (TypeError, ValueError):
        return None
    return time.time() - n * 86400 if n > 0 else None


def _list_filters(qs):
    """Shared by the list and the company cards, so both agree on what 'matching' means."""
    one = lambda k, d="": qs.get(k, [d])[0]
    min_score = one("min_score")
    return {
        "status": one("status") or None,
        "eng_only": one("eng_only", "1") == "1",
        "level_tiers": LEVEL_TIERS.get(one("level", "core"), LEVEL_TIERS["core"]),
        "min_score": float(min_score) if min_score not in ("", "0") else None,
        "q_text": one("q").lower(),
        "company": one("company").lower(),
        "source": one("source"),
        "since_ts": _days_cutoff(one("days")),
    }


def _job_row(r):
    return {**r, "status": r["status"] or "none", "seniority_tier": ats.title_seniority_tier(r["title"])}


def _public_base_url():
    return os.environ.get("JOBSEARCH_BASE_URL", "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- responses
    def _send(self, body, ctype, code=200, headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str).encode(), "application/json", code)

    def _html(self, text, code=200):
        self._send(text.encode(), "text/html; charset=utf-8", code)

    def _redirect(self, location, cookie=None):
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _file(self, full, attach=False):
        ext = os.path.splitext(full)[1]
        ctype = {**CONTENT_TYPES, ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
                 ".json": "application/json"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        extra = [("Content-Disposition", f'attachment; filename="{os.path.basename(full)}"')] if attach else []
        self._send(body, ctype, headers=extra)

    def _static(self, path):
        full = os.path.normpath(os.path.join(STATIC, path.lstrip("/") or "index.html"))
        if not full.startswith(STATIC + os.sep) or not os.path.isfile(full):
            return self.send_error(404)
        self._file(full)

    # -- who is asking
    def _email(self):
        return auth.valid_session_email(self.headers.get("Cookie", ""))

    def _user(self):
        """The users row for the session, or None for a guest / not logged in."""
        email = self._email()
        return db.users_get(email) if email else None

    def _body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise BadRequest("request body is not valid JSON")

    # -- GET
    def do_GET(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    def _guarded(self, fn):
        """One place that turns a failure into a response: bad input is the
        caller's 400, anything else a logged 500 -- never a dropped connection."""
        try:
            fn()
        except BadRequest as e:
            self._json({"error": str(e)}, 400)
        except sqlite3.IntegrityError as e:
            self._json({"error": f"rejected: {e}"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _get(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)

        if p == "/auth/google/login":
            state = secrets.token_urlsafe(24)       # single use: cookie and URL must match on the way back
            return self._redirect(auth.google_auth_redirect_url(state),
                                  f"oauth_state={state}; Max-Age=600; Path=/; HttpOnly; SameSite=Lax")
        if p == "/auth/google/callback":
            code, state = qs.get("code", [""])[0], qs.get("state", [""])[0]
            if not code or not state or state != auth.parse_cookies(self.headers.get("Cookie", "")).get("oauth_state"):
                return self._html('<h1>Login failed</h1><p>Invalid or expired login attempt -- '
                                  'try <a href="/auth/google/login">logging in</a> again.</p>', 400)
            email = auth.google_exchange_code(code)
            if not email:
                return self._html('<h1>Login failed</h1><p>Google did not return a confirmed email -- '
                                  'try <a href="/auth/google/login">logging in</a> again.</p>', 400)
            return self._redirect("/jobs/", f"session={auth.make_session_cookie(email)}; "
                                            f"Max-Age={auth.SESSION_MAX_AGE}; Path=/; HttpOnly; SameSite=Lax")

        if p == "/api/jobs/download":
            return self._download(qs)

        if not self._email():
            return self._redirect("/auth/google/login")
        user = self._user()

        if p == "/api/session":
            return self._json({"email": self._email(), "role": user["role"] if user else "guest"})
        if p == "/":
            return self._redirect("/jobs/")
        if p == "/jobs" or p.startswith("/jobs/"):
            return self._static(p[len("/jobs"):] or "/")
        if not p.startswith("/api/"):
            return self._static(p)

        # A guest (logged in, not whitelisted) may browse the shared postings --
        # user id 0 owns no analysis rows, so they show as having none.
        uid = user["id"] if user else 0

        if p == "/api/jobs/list":
            page = max(1, _int(qs.get("page", ["1"])[0], "page"))
            per_page = max(1, min(100, _int(qs.get("per_page", ["10"])[0], "per_page")))
            matched, total, rows = db.jobs_page(uid, page, per_page, **_list_filters(qs))
            return self._json({"jobs": [_job_row(r) for r in rows], "total_matched": matched,
                               "total_unfiltered": total, "page": page, "per_page": per_page,
                               "total_pages": max(1, (matched + per_page - 1) // per_page)})
        if p == "/api/jobs/companies":
            return self._json({"companies": db.jobs_companies(uid, **_list_filters(qs))})
        if p == "/api/jobs/detail":
            job = db.jobs_get(_int(qs.get("id", ["0"])[0]))
            if not job:
                return self._json({"error": "no such job"}, 404)
            analysis = db.analysis_get(uid, job["id"])
            applied = next((a for a in db.application_all(uid) if a["job_id"] == job["id"]), None)
            return self._json({"job": job, "analysis": analysis, "application": applied})
        if p == "/api/jobs/fetch-status":
            return self._json(worker.status())
        if p == "/api/jobs/missing-clusters":
            return self._json(self._gap_clusters(uid, refresh=qs.get("refresh", ["0"])[0] == "1"))
        if user is None:       # everything below is private data
            return self._json({"error": "Ask the admin to whitelist your account first."}, 403)

        if p == "/api/applications/list":
            return self._json({"applications": db.application_all(uid)})
        if p == "/api/profile/points":
            points = master.point_bank_all(uid, active_only=qs.get("all", ["0"])[0] != "1")
            for pt in points:      # a bracketed draft isn't real resume content yet, nothing to grade
                pt["quality"] = None if pt["text"].lstrip().startswith("[") else ats.check_bullet_brag(pt["text"])
            return self._json({"points": points})
        if p == "/api/profile/skills":
            return self._json({"skills": master.skills_all(uid)})
        if p == "/api/profile/fields":
            return self._json({"profile": master.profile_all(uid)})
        if p == "/api/admin/users":
            if user["role"] != "admin":
                return self._json({"error": "admin only"}, 403)
            return self._json({"users": [{k: u[k] for k in ("email", "role", "added_ts")} for u in db.users_all()]})
        if p == "/api/telegram/config":
            cfg = db.telegram_config_get(uid)       # never echo the token back, only enough to recognise it
            if not cfg:
                return self._json({"configured": False})
            return self._json({"configured": True, "chat_id": cfg["chat_id"], "bot_token_last4": cfg["bot_token"][-4:]})
        self.send_error(404)

    def _gap_clusters(self, uid, refresh):
        """Cached: a tab-open reads the stored clusters; only Refresh pays for the LLM call."""
        cached = db.gap_clusters_get(uid)
        if cached is None or refresh:
            clusters = gaps.cluster_gaps(db.analysis_gaps(uid))
            db.gap_clusters_set(uid, clusters)
            cached = db.gap_clusters_get(uid) or {"clusters": [], "computed_ts": time.time()}
        return {**cached, "scored_count": db.analysis_done_count(uid)}

    def _download(self, qs):
        """A generated file. Authorised by a signed link (Telegram) or a login
        session. The folder always comes from this user's own analysis row,
        never from the URL, so the URL can't name an arbitrary file."""
        one = lambda k: qs.get(k, [""])[0]
        kind = one("kind")
        if kind not in DOWNLOAD_FILES:
            return self._json({"error": "unknown file kind"}, 400)
        job_id = _int(one("job_id"), "job_id")
        if one("sig"):
            uid = _int(one("user"), "user")
            if not links.verify(uid, job_id, kind, one("exp"), one("sig")):
                return self._html("<h1>Invalid or expired link</h1>", 403)
        else:
            user = self._user()
            if user is None:
                return self._json({"error": "not logged in"}, 401)
            uid = user["id"]
        a = db.analysis_get(uid, job_id)
        if not a or a["status"] != "done":
            return self._html("<h1>Not generated yet</h1>", 404)
        full = os.path.normpath(os.path.join(db.DATA_DIR, os.path.dirname(a["resume_path"]), DOWNLOAD_FILES[kind]))
        if not full.startswith(pipeline.FILES_DIR + os.sep) or not os.path.isfile(full):
            return self._html("<h1>File not found</h1>", 404)
        self._file(full, attach=True)

    # -- POST
    def _post(self):
        p = urlparse(self.path).path
        if not self._email():
            return self._json({"error": "not logged in"}, 401)
        b = self._body()
        user = self._user()
        if user is None:
            return self._json({"error": "Ask the admin to whitelist your account first."}, 403)
        uid = user["id"]

        if p.startswith("/api/admin/"):
            if user["role"] != "admin":
                return self._json({"error": "admin only"}, 403)
            return self._admin(p, b)
        if p.startswith("/api/jobs/") or p.startswith("/api/applications/"):
            return self._jobs_post(p, b, uid)
        if p.startswith("/api/profile/"):
            return self._profile_post(p, b, uid)
        if p.startswith("/api/telegram/"):
            return self._telegram(p, b, uid)
        self.send_error(404)

    def _jobs_post(self, p, b, uid):
        if p == "/api/jobs/manual":
            company, title = (b.get("company") or "").strip(), (b.get("title") or "").strip()
            text, url, location = (b.get("text") or "").strip(), (b.get("url") or "").strip(), (b.get("location") or "").strip()
            if not company or not title or not text:
                raise BadRequest("company, title, and text are required")
            if len(title) > MAX_TITLE_LEN:     # the description ended up in the title field
                raise BadRequest(f"title is implausibly long ({len(title)} chars) -- "
                                 "looks like the description ended up in the title field")
            job_id, is_new = db.jobs_add("manual", db.manual_external_id(company, title, text),
                                         company, title, url, text, location)
            db.analysis_ensure(uid, job_id)
            queued = worker.enqueue(uid, job_id, manual=True)
            return self._json({"job_id": job_id, "is_new": is_new, "queued": queued})

        if p in ("/api/jobs/regenerate", "/api/jobs/clear"):
            job_id = _int(b.get("job_id"), "job_id")
            if not db.jobs_get(job_id):
                return self._json({"error": "no such job"}, 404)
            db.analysis_ensure(uid, job_id)
            regenerate = p.endswith("regenerate")
            ok, old_files = (db.analysis_regenerate if regenerate else db.analysis_clear)(uid, job_id)
            if not ok:
                return self._json({"error": "this job is being processed right now -- try again when it finishes"}, 409)
            pipeline.remove_files(old_files)
            if regenerate:
                worker.enqueue(uid, job_id, manual=True)
            return self._json({"ok": True})

        if p == "/api/jobs/mark-applied":
            job_id = _int(b.get("job_id"), "job_id")
            if not db.jobs_get(job_id):
                return self._json({"error": "no such job"}, 404)
            app_id = db.application_mark_applied(uid, job_id, b.get("via", "other"),
                                                 b.get("referral_name", ""), b.get("referral_note", ""))
            return self._json({"ok": True, "application_id": app_id})

        if p == "/api/jobs/refetch":
            if not worker.fetch_now():
                return self._json({"error": "A fetch is already running"}, 409)
            return self._json({"started": True})

        if p == "/api/applications/add-status":
            try:
                db.application_add_event(uid, _int(b.get("app_id"), "app_id"), b.get("status", ""), b.get("note", ""))
            except ValueError as e:
                raise BadRequest(str(e))
            return self._json({"ok": True})
        self.send_error(404)

    def _profile_post(self, p, b, uid):
        """The master document is edited by the user and nobody else: every
        route here is a plain write to the user's JSON file, no LLM."""
        try:
            return self._master_edit(p, b, uid)
        except ValueError as e:                 # master.py's own message says what is wrong
            raise BadRequest(str(e))

    def _master_edit(self, p, b, uid):
        if p == "/api/profile/points/add":
            tags = b.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if b.get("entry_type", "role") not in master.ENTRY_TYPES:
                raise BadRequest(f"entry_type must be one of {master.ENTRY_TYPES}")
            for key in ("company", "role", "text"):
                if not (b.get(key) or "").strip():
                    raise BadRequest(f"{key} is required")
            pid = master.point_bank_add(uid, b["company"], b["role"], b.get("start_date", ""), b.get("end_date"),
                                        b["text"], tags=tags, entry_type=b.get("entry_type", "role"))
            return self._json({"ok": True, "id": pid})

        if p == "/api/profile/points/update":
            text = b.get("text", "")
            master.point_bank_update_text(uid, _int(b.get("id")), text)
            quality = ats.check_bullet_brag(text)
            return self._json({"ok": True, "quality_warning": None if quality["ok"] else quality})

        if p == "/api/profile/points/toggle":
            master.point_bank_set_active(uid, _int(b.get("id")), bool(b.get("active", True)))
            return self._json({"ok": True})

        if p == "/api/profile/skills/add":
            master.skills_add(uid, b.get("name", ""), b.get("category", "Other"))
            return self._json({"ok": True})

        if p == "/api/profile/fields/set":
            master.profile_set(uid, b.get("key", ""), b.get("value", ""))
            return self._json({"ok": True})
        self.send_error(404)

    def _admin(self, p, b):
        if p == "/api/admin/users/add":
            email = (b.get("email") or "").strip().lower()
            if "@" not in email:
                raise BadRequest("a real email address is required")
            db.users_add(email)
        elif p == "/api/admin/users/remove":
            email = (b.get("email") or "").strip().lower()
            user = db.users_get(email)
            db.users_remove(email)
            if user and user["role"] != "admin":     # admins can't be removed, so keep their document
                master.delete(user["id"])
        else:
            return self.send_error(404)
        self._json({"users": [{k: u[k] for k in ("email", "role", "added_ts")} for u in db.users_all()]})

    def _telegram(self, p, b, uid):
        if p == "/api/telegram/config":
            token, chat = (b.get("bot_token") or "").strip(), (b.get("chat_id") or "").strip()
            if not token or not chat:
                raise BadRequest("bot token and chat id are both required")
            db.telegram_config_set(uid, token, chat)
            return self._json({"ok": True})
        if p == "/api/telegram/test":
            cfg = db.telegram_config_get(uid)
            if not cfg:
                raise BadRequest("no Telegram config saved yet")
            ok, err = notify_telegram.send_test_message(cfg["bot_token"], cfg["chat_id"])
            if not ok:
                raise BadRequest(err)
            return self._json({"ok": True})
        self.send_error(404)


def main():
    auth.check_config()
    db.connect().close()          # creates the schema on first run
    worker.start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"jobsearch  ->  http://{HOST}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
