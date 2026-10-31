"""State for the job application system. Multi-user, SQLite, no ORM, no JSON
columns -- see schema.sql for the tables and the flow they encode.

Two rules every function here follows:
    - one statement that must be atomic is one statement (the queue claim is a
      single conditional UPDATE, never a read-then-write);
    - anything touching more than one table runs inside tx(), so a reader
      never sees a half-written result (an analysis marked done without its
      requirements, a point without its tags).

Everything private is keyed by user_id, never by email. `jobs` is the one
shared table: public listings, one row per posting.
"""

import contextlib
import difflib
import hashlib
import os
import re
import sqlite3
import time

import ats

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("JOBSEARCH_DATA") or os.path.join(ROOT, "data")
DB_PATH = os.environ.get("JOBSEARCH_DB") or os.path.join(DATA_DIR, "app.db")
SCHEMA_PATH = os.path.join(ROOT, "schema.sql")
MAX_ATTEMPTS = 3

# Seeds the first admin so a fresh database is never locked out of its own
# app. Same env var the login gate has always used.
ADMIN_EMAIL = (os.environ.get("ALLOWED_LOGIN_EMAIL") or "admin@local").lower()

_ready = False


def _init(con):
    global _ready
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with open(SCHEMA_PATH) as f:
        con.executescript(f.read())
    con.execute("INSERT OR IGNORE INTO users (email, role, added_ts) VALUES (?, 'admin', ?)",
                (ADMIN_EMAIL, time.time()))
    _ready = True


def connect():
    """Autocommit connection (isolation_level=None): a bare execute() is its
    own transaction, and multi-statement work opts in via tx(). Foreign keys
    are per-connection in SQLite, so they're switched on here every time."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    # The same classifiers ats.py already has, callable from SQL so the
    # Postings filter runs in the WHERE clause instead of a Python loop.
    con.create_function("IS_ENGINEERING_TITLE", 1,
                        lambda title: 1 if ats.is_likely_engineering_title(title or "") else 0)
    con.create_function("SENIORITY_TIER", 1, lambda title: ats.title_seniority_tier(title or ""))
    if not _ready:
        _init(con)
    return con


@contextlib.contextmanager
def tx():
    """BEGIN IMMEDIATE ... COMMIT, rolled back if the body raises."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()


def _all(sql, *params):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def _one(sql, *params):
    rows = _all(sql, *params)
    return rows[0] if rows else None


def _run(sql, *params):
    """One autocommit statement. Returns rowcount."""
    con = connect()
    try:
        return con.execute(sql, params).rowcount
    finally:
        con.close()


# ── users ───────────────────────────────────────────────────────────────
def users_all():
    return _all("SELECT * FROM users ORDER BY role, added_ts")


def users_get(email):
    return _one("SELECT * FROM users WHERE email = ?", (email or "").strip().lower())


def users_add(email):
    """Whitelist one email. Re-adding is a no-op. Never grants admin."""
    _run("INSERT INTO users (email, added_ts) VALUES (?, ?) ON CONFLICT(email) DO NOTHING",
         email.strip().lower(), time.time())
    return users_get(email)


def users_remove(email):
    """Deletes the user and, by cascade, everything they own. Admins can't be removed."""
    _run("DELETE FROM users WHERE email = ? AND role != 'admin'", email.strip().lower())


# ── telegram ────────────────────────────────────────────────────────────
def telegram_config_get(user_id):
    return _one("SELECT * FROM telegram_config WHERE user_id = ?", user_id)


def telegram_config_set(user_id, bot_token, chat_id):
    _run("INSERT INTO telegram_config (user_id, bot_token, chat_id, updated_ts) VALUES (?,?,?,?) "
         "ON CONFLICT(user_id) DO UPDATE SET bot_token=excluded.bot_token, "
         "chat_id=excluded.chat_id, updated_ts=excluded.updated_ts",
         user_id, bot_token, chat_id, time.time())


# ── profile and skills ──────────────────────────────────────────────────
def profile_all(user_id):
    return {r["key"]: r["value"] for r in _all("SELECT key, value FROM profile WHERE user_id = ?", user_id)}


def profile_set(user_id, key, value):
    _run("INSERT INTO profile (user_id, key, value) VALUES (?,?,?) "
         "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value", user_id, key, value)


def skills_all(user_id, active_only=True):
    q = "SELECT name, category, active FROM skills WHERE user_id = ?"
    return _all(q + (" AND active = 1" if active_only else "") + " ORDER BY category, name", user_id)


def skills_add(user_id, name, category):
    """Name is case-insensitive in the schema, so a re-add with different
    casing updates the one existing row and keeps its original spelling."""
    _run("INSERT INTO skills (user_id, name, category) VALUES (?,?,?) "
         "ON CONFLICT(user_id, name) DO UPDATE SET category=excluded.category, active=1",
         user_id, name, category)


# ── point bank ──────────────────────────────────────────────────────────
def point_bank_add(user_id, company, role, start_date, end_date, text, tags=(), entry_type="role"):
    with tx() as con:
        pid = con.execute(
            "INSERT INTO point_bank (user_id, entry_type, company, role, start_date, end_date, text) "
            "VALUES (?,?,?,?,?,?,?)", (user_id, entry_type, company, role, start_date, end_date, text)).lastrowid
        con.executemany("INSERT INTO point_tag (point_id, tag) VALUES (?,?) ON CONFLICT DO NOTHING",
                        [(pid, t) for t in tags if t])
    return pid


def point_bank_all(user_id, active_only=True):
    rows = _all(
        "SELECT p.*, (SELECT group_concat(tag, char(31)) FROM point_tag t WHERE t.point_id = p.id) AS tag_list "
        "FROM point_bank p WHERE p.user_id = ?" + (" AND p.active = 1" if active_only else "") +
        " ORDER BY p.start_date DESC, p.id", user_id)
    for r in rows:
        tag_list = r.pop("tag_list")
        r["tags"] = sorted(tag_list.split("\x1f")) if tag_list else []
    return rows


def point_bank_find_similar(user_id, text, threshold=0.82):
    """Best near-duplicate already in this user's point bank, as (row, ratio),
    or (None, 0.0). Catches a second resume variant re-extracting the same
    accomplishment reworded slightly."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    best, best_ratio = None, 0.0
    for row in point_bank_all(user_id, active_only=False):
        ratio = difflib.SequenceMatcher(None, norm, re.sub(r"\s+", " ", row["text"].strip().lower())).ratio()
        if ratio > best_ratio:
            best, best_ratio = row, ratio
    return (best, best_ratio) if best and best_ratio >= threshold else (None, 0.0)


def point_bank_set_active(user_id, pid, active):
    _run("UPDATE point_bank SET active = ? WHERE id = ? AND user_id = ?", 1 if active else 0, pid, user_id)


def point_bank_update_text(user_id, pid, text):
    _run("UPDATE point_bank SET text = ? WHERE id = ? AND user_id = ?", text, pid, user_id)


def point_bank_update_entry_type(user_id, pid, entry_type, role=None):
    """Reclassify between 'role' and 'project'; `role` optionally rewrites the
    role/tagline column in the same statement."""
    _run("UPDATE point_bank SET entry_type = ?, role = COALESCE(?, role) WHERE id = ? AND user_id = ?",
         entry_type, role, pid, user_id)


# ── jobs (shared) ───────────────────────────────────────────────────────
def manual_external_id(company, title, text):
    """A manual job has no ATS id, so its identity is its content: pasting the
    same posting twice lands on the same (company, external_id) row."""
    norm = re.sub(r"\s+", " ", f"{company}|{title}|{text}".strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def jobs_add(source, external_id, company, title, url, description_raw, location="", posted_ts=None):
    """Returns (job_id, is_new). A posting seen before only has last_seen_ts
    bumped (and location filled in if it was blank) -- nothing is inserted."""
    now = time.time()
    with tx() as con:
        cur = con.execute(
            "INSERT INTO jobs (source, external_id, company, title, url, description_raw, location, "
            "posted_ts, first_seen_ts, last_seen_ts) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(company, external_id) DO NOTHING",
            (source, external_id, company, title, url, description_raw, location, posted_ts, now, now))
        if cur.rowcount:
            return cur.lastrowid, True
        con.execute("UPDATE jobs SET last_seen_ts = ?, location = CASE WHEN location = '' THEN ? ELSE location END "
                    "WHERE company = ? AND external_id = ?", (now, location, company, external_id))
        row = con.execute("SELECT id FROM jobs WHERE company = ? AND external_id = ?",
                          (company, external_id)).fetchone()
        return row["id"], False


def jobs_get(job_id):
    return _one("SELECT * FROM jobs WHERE id = ?", job_id)


_LIST_COLS = ("j.id, j.source, j.company, j.title, j.url, j.location, j.posted_ts, j.first_seen_ts, "
              "j.last_seen_ts, a.status, a.score, a.reason, a.attempts, a.error_kind, a.error_msg, "
              "a.started_ts, a.finished_ts")


def _job_filter(user_id, status=None, eng_only=True, level_tiers=None, min_score=None, q_text="",
                company="", source="", since_ts=None, exclude_applied=True):
    """FROM/WHERE for the Postings list, joined to THIS user's analysis row.
    status 'none' means no analysis row at all. A manual job always passes the
    engineering-title filter -- a job added by hand is wanted by definition."""
    where, params = ["1=1"], [user_id]
    if status == "none":
        where.append("a.status IS NULL")
    elif status:
        where.append("a.status = ?")
        params.append(status)
    if eng_only:
        where.append("(j.source = 'manual' OR IS_ENGINEERING_TITLE(j.title) = 1)")
    if level_tiers:
        where.append(f"SENIORITY_TIER(j.title) IN ({','.join('?' * len(level_tiers))})")
        params.extend(level_tiers)
    if min_score is not None:
        # An unscored posting always passes -- "score >= ?" alone would drop
        # every one of them, since NULL compares false.
        where.append("(a.score IS NULL OR a.score >= ?)")
        params.append(float(min_score))
    if q_text:
        where.append("(LOWER(j.company) LIKE ? OR LOWER(j.title) LIKE ?)")
        params.extend([f"%{q_text}%"] * 2)
    if company:
        where.append("LOWER(j.company) = ?")
        params.append(company)
    if source == "manual":
        where.append("j.source = 'manual'")
    elif source == "auto":
        where.append("j.source != 'manual'")
    if since_ts is not None:
        where.append("j.first_seen_ts >= ?")
        params.append(since_ts)
    if exclude_applied:
        where.append("NOT EXISTS (SELECT 1 FROM application p WHERE p.user_id = ? AND p.job_id = j.id)")
        params.append(user_id)
    return "FROM jobs j LEFT JOIN analysis a ON a.job_id = j.id AND a.user_id = ? WHERE " + " AND ".join(where), params


def jobs_page(user_id, page=1, per_page=10, **filters):
    """(total_matched, total_unfiltered, rows) for one page, newest first."""
    frm, params = _job_filter(user_id, **filters)
    total = _one(f"SELECT COUNT(*) AS n {frm}", *params)["n"]
    rows = _all(f"SELECT {_LIST_COLS} {frm} ORDER BY j.first_seen_ts DESC, a.score DESC NULLS LAST, j.id DESC "
                "LIMIT ? OFFSET ?", *params, per_page, (page - 1) * per_page)
    return total, _one("SELECT COUNT(*) AS n FROM jobs")["n"], rows


def jobs_companies(user_id, **filters):
    """One row per company over the same filter: posting count, best score,
    and the title that earned it."""
    frm, params = _job_filter(user_id, **filters)
    return _all(
        f"SELECT j.company, COUNT(*) AS count, MAX(a.score) AS best_score, "
        f"(SELECT j2.title FROM jobs j2 JOIN analysis a2 ON a2.job_id = j2.id AND a2.user_id = ? "
        f" WHERE j2.company = j.company ORDER BY a2.score DESC NULLS LAST LIMIT 1) AS best_title "
        f"{frm} GROUP BY j.company ORDER BY best_score DESC NULLS LAST, j.company", user_id, *params)


# ── analysis: the queue's source of truth ───────────────────────────────
def analysis_ensure(user_id, job_id):
    """Make sure this user has a work item for this job. Never touches an
    existing one -- the (user_id, job_id) key is the whole dedup."""
    return _run("INSERT INTO analysis (user_id, job_id, created_ts) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                user_id, job_id, time.time())


def analysis_enqueue_missing(since_ts, users=None):
    """For each user (default: all), a pending analysis row for each eligible
    posting first seen at or after since_ts that they don't have one for yet.
    Idempotent: run it after every fetch. Manual jobs are skipped here --
    they're enqueued explicitly by whoever added them. Returns how many rows
    were created."""
    created = 0
    for user in (users if users is not None else users_all()):
        # is_target_location only reads the first 500 chars, so that's all we load
        rows = _all(
            "SELECT j.id, j.title, j.location, substr(j.description_raw, 1, 500) AS head FROM jobs j "
            "WHERE j.first_seen_ts >= ? AND j.source != 'manual' AND NOT EXISTS "
            "(SELECT 1 FROM analysis a WHERE a.user_id = ? AND a.job_id = j.id)", since_ts, user["id"])
        now = time.time()
        ids = [(user["id"], r["id"], now) for r in rows if ats.is_eligible(r["title"], r["location"], r["head"])]
        with tx() as con:
            con.executemany("INSERT INTO analysis (user_id, job_id, created_ts) VALUES (?,?,?) "
                            "ON CONFLICT DO NOTHING", ids)
        created += len(ids)
    return created


def analysis_pending(limit=200):
    """Rows the poller may claim: pending or retry, manual jobs first, then
    newest first."""
    return _all(
        "SELECT a.user_id, a.job_id, (j.source = 'manual') AS manual FROM analysis a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.status IN ('pending', 'retry') AND a.attempts < ? "
        "ORDER BY manual DESC, j.first_seen_ts DESC LIMIT ?", MAX_ATTEMPTS, limit)


def analysis_claim(user_id, job_id):
    """pending|retry -> in_process, counting the attempt. One conditional
    UPDATE, so of any number of claimers exactly one gets True."""
    return _run("UPDATE analysis SET status = 'in_process', attempts = attempts + 1, queued_ts = ? "
                "WHERE user_id = ? AND job_id = ? AND status IN ('pending', 'retry') AND attempts < ?",
                time.time(), user_id, job_id, MAX_ATTEMPTS) == 1


def analysis_start(user_id, job_id):
    _run("UPDATE analysis SET started_ts = ? WHERE user_id = ? AND job_id = ?", time.time(), user_id, job_id)


def analysis_attempt(user_id, job_id):
    row = _one("SELECT attempts FROM analysis WHERE user_id = ? AND job_id = ?", user_id, job_id)
    return row["attempts"] if row else 0


def analysis_step_add(user_id, job_id, attempt, step, started_ts, duration_ms, ok, provider=None,
                      usage=None, error=None):
    """One pipeline step's timing and token usage. Written as each step ends,
    so a failed attempt still leaves its metrics behind."""
    u = usage or {}
    _run("INSERT OR REPLACE INTO analysis_step (user_id, job_id, attempt, step, started_ts, duration_ms, "
         "provider, prompt_tokens, completion_tokens, reasoning_tokens, cached_tokens, ok, error) "
         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
         user_id, job_id, attempt, step, started_ts, duration_ms, provider, u.get("prompt_tokens"),
         u.get("completion_tokens"), u.get("reasoning_tokens"), u.get("cached_tokens"), 1 if ok else 0, error)


def analysis_complete(user_id, job_id, score, reason, provider, resume_path, cover_path,
                      requirements, gaps, projects, items):
    """in_process -> done together with every detail row, in one transaction.
    Raises (and writes nothing) if the row is no longer in_process."""
    with tx() as con:
        for t in ("analysis_requirement", "analysis_gap", "analysis_project", "analysis_item"):
            con.execute(f"DELETE FROM {t} WHERE user_id = ? AND job_id = ?", (user_id, job_id))
        # evidence_point_id is only kept if it's a real row in THIS user's point bank
        con.executemany(
            "INSERT INTO analysis_requirement (user_id, job_id, pos, requirement, importance, presence, depth, "
            "evidence_point_id, weight, earned) VALUES (?,?,?,?,?,?,?,"
            "(SELECT id FROM point_bank WHERE id = ? AND user_id = ?),?,?)",
            [(user_id, job_id, i, r["requirement"], r["importance"], r["presence"], r["depth"],
              r.get("evidence_point_id"), user_id, r["weight"], r["earned"]) for i, r in enumerate(requirements)])
        con.executemany("INSERT INTO analysis_gap (user_id, job_id, pos, gap, why, example) VALUES (?,?,?,?,?,?)",
                        [(user_id, job_id, i, g["gap"], g.get("why", ""), g.get("example", ""))
                         for i, g in enumerate(gaps)])
        con.executemany("INSERT INTO analysis_project (user_id, job_id, point_id, worth_including, reason) "
                        "VALUES (?,?,?,?,?)",
                        [(user_id, job_id, p["point_id"], 1 if p["worth_including"] else 0, p.get("reason", ""))
                         for p in projects])
        con.executemany("INSERT INTO analysis_item (user_id, job_id, pos, point_id, kind, text) VALUES (?,?,?,?,?,?)",
                        [(user_id, job_id, i, it["point_id"], it["kind"], it["text"]) for i, it in enumerate(items)])
        cur = con.execute(
            "UPDATE analysis SET status = 'done', score = ?, reason = ?, provider = ?, resume_path = ?, "
            "cover_path = ?, finished_ts = ?, error_kind = NULL, error_msg = NULL "
            "WHERE user_id = ? AND job_id = ? AND status = 'in_process'",
            (score, reason, provider, resume_path, cover_path, time.time(), user_id, job_id))
        if cur.rowcount != 1:
            raise RuntimeError("analysis is no longer in_process; nothing was saved")


def analysis_fail(user_id, job_id, kind, msg, permanent=False):
    """in_process -> retry, or -> failed once the attempts are used up (or
    immediately for a permanent error that retrying can't fix). Returns the
    status it landed on."""
    now = time.time()
    with tx() as con:
        row = con.execute("SELECT attempts FROM analysis WHERE user_id = ? AND job_id = ? AND status = 'in_process'",
                          (user_id, job_id)).fetchone()
        if row is None:
            return None
        final = permanent or row["attempts"] >= MAX_ATTEMPTS
        status = "failed" if final else "retry"
        con.execute("UPDATE analysis SET status = ?, error_kind = ?, error_msg = ?, finished_ts = ? "
                    "WHERE user_id = ? AND job_id = ?",
                    (status, kind, msg[:500], now if final else None, user_id, job_id))
    return status


def analysis_reset_interrupted():
    """Startup, single process: whatever was in_process died with the previous
    one. Back to retry, or failed if that was its last attempt. Returns count."""
    now = time.time()
    with tx() as con:
        con.execute("UPDATE analysis SET status = 'failed', error_kind = 'interrupted', "
                    "error_msg = 'server restarted mid-run', finished_ts = ? "
                    "WHERE status = 'in_process' AND attempts >= ?", (now, MAX_ATTEMPTS))
        return con.execute("UPDATE analysis SET status = 'retry' WHERE status = 'in_process'").rowcount


def _reset(con, user_id, job_id, status):
    """Wipe one analysis back to a blank row with this status. Returns
    (ok, old_folder_paths). Refuses while a worker has it."""
    row = con.execute("SELECT status, resume_path, cover_path FROM analysis WHERE user_id = ? AND job_id = ?",
                      (user_id, job_id)).fetchone()
    if row and row["status"] == "in_process":
        return False, []
    old = [p for p in (row["resume_path"], row["cover_path"]) if p] if row else []
    for t in ("analysis_requirement", "analysis_gap", "analysis_project", "analysis_item", "analysis_step"):
        con.execute(f"DELETE FROM {t} WHERE user_id = ? AND job_id = ?", (user_id, job_id))
    con.execute(
        "INSERT INTO analysis (user_id, job_id, status, created_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id, job_id) DO UPDATE SET status = excluded.status, created_ts = excluded.created_ts, "
        "attempts = 0, error_kind = NULL, error_msg = NULL, queued_ts = NULL, started_ts = NULL, "
        "finished_ts = NULL, score = NULL, reason = NULL, provider = NULL, resume_path = NULL, cover_path = NULL",
        (user_id, job_id, status, time.time()))
    return True, old


def analysis_regenerate(user_id, job_id):
    """Back to pending with all results dropped. (ok, old_files) -- the caller
    deletes the files after this commits."""
    with tx() as con:
        return _reset(con, user_id, job_id, "pending")


def analysis_clear(user_id, job_id):
    """Drop results and files but don't queue anything: status 'cleared'."""
    with tx() as con:
        return _reset(con, user_id, job_id, "cleared")


def analysis_get(user_id, job_id):
    """The analysis row with its requirements, gaps, projects and steps, or None."""
    a = _one("SELECT * FROM analysis WHERE user_id = ? AND job_id = ?", user_id, job_id)
    if a is None:
        return None
    key = (user_id, job_id)
    a["requirements"] = _all("SELECT * FROM analysis_requirement WHERE user_id = ? AND job_id = ? ORDER BY pos", *key)
    a["gaps"] = _all("SELECT gap, why, example FROM analysis_gap WHERE user_id = ? AND job_id = ? ORDER BY pos", *key)
    a["projects"] = _all("SELECT ap.point_id, p.company AS name, ap.worth_including, ap.reason FROM analysis_project ap "
                         "JOIN point_bank p ON p.id = ap.point_id WHERE ap.user_id = ? AND ap.job_id = ?", *key)
    a["steps"] = _all("SELECT * FROM analysis_step WHERE user_id = ? AND job_id = ? ORDER BY attempt, started_ts", *key)
    return a


def analysis_done_count(user_id):
    return _one("SELECT COUNT(*) AS n FROM analysis WHERE user_id = ? AND status = 'done'", user_id)["n"]


def analysis_gaps(user_id):
    """Every (job_id, gap) this user has, for clustering."""
    return _all("SELECT job_id, gap FROM analysis_gap WHERE user_id = ? AND gap != '' ORDER BY job_id, pos", user_id)


def gap_clusters_set(user_id, clusters):
    """Replace this user's clusters: [{label, job_ids}]. All or nothing."""
    now = time.time()
    with tx() as con:
        con.execute("DELETE FROM gap_cluster WHERE user_id = ?", (user_id,))
        for c in clusters:
            cid = con.execute("INSERT INTO gap_cluster (user_id, label, computed_ts) VALUES (?,?,?)",
                              (user_id, c["label"], now)).lastrowid
            con.executemany("INSERT INTO gap_cluster_job (cluster_id, job_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                            [(cid, j) for j in c["job_ids"]])


def gap_clusters_get(user_id):
    """{clusters: [{label, count, job_ids}], computed_ts}, or None if never computed."""
    rows = _all("SELECT c.id, c.label, c.computed_ts, j.job_id FROM gap_cluster c "
                "LEFT JOIN gap_cluster_job j ON j.cluster_id = c.id WHERE c.user_id = ? ORDER BY c.id", user_id)
    if not rows:
        return None
    by_id = {}
    for r in rows:
        c = by_id.setdefault(r["id"], {"label": r["label"], "job_ids": []})
        if r["job_id"] is not None:
            c["job_ids"].append(r["job_id"])
    clusters = [{**c, "count": len(c["job_ids"])} for c in by_id.values()]
    return {"clusters": sorted(clusters, key=lambda c: -c["count"]), "computed_ts": rows[0]["computed_ts"]}


# ── applications: the user's own funnel ─────────────────────────────────
def application_mark_applied(user_id, job_id, applied_via="other", referral_name="", referral_note=""):
    """Idempotent: marking the same job twice returns the first application's
    id instead of creating a second."""
    now = time.time()
    with tx() as con:
        cur = con.execute(
            "INSERT INTO application (user_id, job_id, applied_via, applied_ts, referral_name, referral_note) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, job_id) DO NOTHING",
            (user_id, job_id, applied_via, now, referral_name, referral_note))
        if cur.rowcount:
            con.execute("INSERT INTO application_event (application_id, seq, ts, status, note) VALUES (?,1,?,?,?)",
                        (cur.lastrowid, now, "applied", f"via {applied_via}"))
            return cur.lastrowid
        return con.execute("SELECT id FROM application WHERE user_id = ? AND job_id = ?",
                           (user_id, job_id)).fetchone()["id"]


def application_add_event(user_id, app_id, status, note=""):
    """Append a funnel stage and make it the current status. Raises if the
    application isn't this user's."""
    with tx() as con:
        if con.execute("SELECT 1 FROM application WHERE id = ? AND user_id = ?", (app_id, user_id)).fetchone() is None:
            raise ValueError(f"no application with id={app_id} for this user")
        con.execute("INSERT INTO application_event (application_id, seq, ts, status, note) "
                    "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM application_event WHERE application_id = ?), ?, ?, ?)",
                    (app_id, app_id, time.time(), status, note))
        con.execute("UPDATE application SET status = ? WHERE id = ?", (status, app_id))


def application_all(user_id):
    """This user's applications, newest first, each with its events and
    whether generated files exist for the job."""
    apps = _all(
        "SELECT p.*, j.company, j.title, a.score, (a.status = 'done') AS has_files FROM application p "
        "JOIN jobs j ON j.id = p.job_id LEFT JOIN analysis a ON a.job_id = p.job_id AND a.user_id = p.user_id "
        "WHERE p.user_id = ? ORDER BY p.applied_ts DESC", user_id)
    events = _all("SELECT e.* FROM application_event e JOIN application p ON p.id = e.application_id "
                  "WHERE p.user_id = ? ORDER BY e.application_id, e.seq", user_id)
    for a in apps:
        a["events"] = [e for e in events if e["application_id"] == a["id"]]
    return apps
