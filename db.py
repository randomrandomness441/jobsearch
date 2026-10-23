"""State for the job application system. Multi-user, SQLite, no ORM.

Same idiom as the C++ prep platform's db.py: plain stdlib sqlite3, no
framework, no ORM. Two things live here that don't in a normal CRUD app:

    point_bank   the source-of-truth career facts every generated resume
                 and cover letter pulls from -- never overwritten silently,
                 `active` flags a row as retired instead of deleting it, so
                 history and reasoning stay inspectable.
    applications the record of what was actually generated and where it
                 landed on disk, so nothing generated is ever orphaned from
                 its own folder.

Multi-user shape: `jobs` (the fetched postings) is the one table shared by
everyone -- public listings, not private data, so one fetch serves every
user. Everything else is keyed by user_email: each person's own
point_bank/skills/profile, their own job_user_state (score/status/
notified, per job), their own applications. See `users` for who's allowed
to use the app at all (admin-managed whitelist; a confirmed Google login
that isn't on it is a guest -- identified, but with no rows anywhere in
the per-user tables, so they see the shared postings with nothing
personalized and no real actions).
"""

import difflib
import hashlib
import json
import os
import re
import sqlite3
import time
import importlib.util as _importlib_util

# Not a plain "import ats" -- when this file is loaded by server.py's own
# _load_module (importlib.util.spec_from_file_location, registered as
# "jobsearch_db", not "db"), a plain import ats can't resolve: jobsearch/
# is never added to sys.path, so Python looks for "ats" on platform/'s own
# sys.path and doesn't find it -- confirmed this breaks server.py startup
# before relying on it. Loading by this file's own directory instead works
# identically whether db.py is run directly (python3 fetch_ats.py, cwd=
# jobsearch/) or loaded by server.py's mechanism -- no sys.path dependency
# either way.
_ats_spec = _importlib_util.spec_from_file_location(
    "jobsearch_ats_for_db", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ats.py"))
ats = _importlib_util.module_from_spec(_ats_spec)
_ats_spec.loader.exec_module(ats)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "jobsearch.db")

# Only used once, by _migrate(), to seed the first admin and to decide whose
# email pre-existing (pre-multi-user) rows belong to -- every table that used
# to be implicitly "the one candidate" becomes per-user here, and existing
# data has to land under SOMEONE's email rather than vanish. Same env var
# the old single-user login gate used, so a deploy that already has it set
# needs no new configuration.
ADMIN_EMAIL = os.environ.get("ALLOWED_LOGIN_EMAIL", "") or "admin@local"

SCHEMA = """
-- Multi-user gate: who's allowed to actually use the app. 'admin' (just
-- you -- seeded from ALLOWED_LOGIN_EMAIL, the env var that used to be the
-- entire auth system) can manage this list; 'whitelisted' is anyone the
-- admin added. Anyone who logs in via Google but ISN'T in this table is
-- a guest -- identified, but every private table below is keyed by
-- email, so a guest simply has no rows anywhere and sees the shared job
-- postings with nothing personalized, no real actions.
CREATE TABLE IF NOT EXISTS users (
    email    TEXT PRIMARY KEY,
    role     TEXT NOT NULL DEFAULT 'whitelisted',  -- admin|whitelisted
    added_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS point_bank (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email  TEXT NOT NULL,         -- whose master resume this belongs to
    company     TEXT NOT NULL,
    role        TEXT NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT,                  -- NULL = current role
    text        TEXT NOT NULL,         -- the accomplishment, as written
    tags        TEXT DEFAULT '[]',     -- JSON array of skill/keyword tags
    metrics     TEXT DEFAULT '[]',     -- JSON array of quantified numbers in text
    source_note TEXT DEFAULT '',       -- which resume file this came from
    active      INTEGER DEFAULT 1      -- 0 = retired/superseded, kept for history
);

CREATE TABLE IF NOT EXISTS skills (
    user_email TEXT NOT NULL,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    active     INTEGER DEFAULT 1,
    PRIMARY KEY (user_email, name)
);

CREATE TABLE IF NOT EXISTS profile (
    user_email TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (user_email, key)
);

-- Shared across every user -- these are public job listings pulled from
-- company ATS feeds, not private data, so one fetch serves everyone
-- instead of each user redundantly hitting the same APIs. What's
-- personal (score, status, notified) lives in job_user_state below, one
-- row per (user, job) instead of columns on this table.
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,          -- greenhouse|lever|pinloop|manual
    company          TEXT NOT NULL,
    title            TEXT NOT NULL,
    url              TEXT DEFAULT '',
    description_raw  TEXT NOT NULL,
    fetched_ts       REAL NOT NULL,
    match_score      REAL,
    match_reason     TEXT DEFAULT '',
    content_hash     TEXT DEFAULT '',
    location         TEXT DEFAULT '',
    posted_ts        REAL
);

-- One user's relationship to one shared posting: their own AI-judge
-- score, their own queued/applied status, their own notification state.
-- Two users looking at the same job_id never see each other's row here.
CREATE TABLE IF NOT EXISTS job_user_state (
    user_email       TEXT NOT NULL,
    job_id           INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'new',  -- new|queued|applied|skipped
    llm_score        REAL,
    llm_reason       TEXT DEFAULT '',
    llm_judged_ts    REAL,
    llm_missing      TEXT DEFAULT '[]',
    llm_projects     TEXT DEFAULT '[]',
    llm_requirements TEXT DEFAULT '[]',
    notified_ts      REAL,
    PRIMARY KEY (user_email, job_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email        TEXT NOT NULL,         -- whose generated materials these are
    job_id            INTEGER NOT NULL,
    resume_path       TEXT,
    cover_letter_path TEXT,
    ats_estimate      TEXT DEFAULT '{}',      -- JSON: {score, breakdown: [...]}
    generated_ts      REAL NOT NULL,
    folder_path       TEXT NOT NULL,
    applied_via       TEXT DEFAULT '',        -- company_site|linkedin|referral|recruiter|other
    applied_ts        REAL,                   -- NULL until actually submitted
    referral_name     TEXT DEFAULT '',
    referral_note     TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'generated',
        -- generated|applied|phone_screen|interview|offer|rejected|ghosted|withdrawn
    status_history    TEXT DEFAULT '[]'       -- JSON [{status, ts, note}, ...]
);

-- A batch's live progress used to live only in server.py's process memory
-- -- a real restart (deploying a code change, say) wiped it blank with no
-- way to tell "nothing's ever run" from "something ran and finished" from
-- "something was running when the server went down". One row per user --
-- each person's own AI-judge batch run, not a shared single row.
CREATE TABLE IF NOT EXISTS judge_progress (
    user_email TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);

-- The Gaps tab's cluster result, same per-user shape as judge_progress
-- above -- was recomputing with a real LLM call every single time the tab
-- was opened, not just on an explicit Refresh. This is that cache: the
-- tab reads it instantly, and only the Refresh button (or a scoped
-- rejudge finishing) writes a fresh one. Per user, since it's built from
-- that user's own point bank and their own job scores.
CREATE TABLE IF NOT EXISTS gap_clusters (
    user_email TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);

-- One Telegram bot/chat per user, keyed by their login email -- the
-- PRIMARY KEY is what makes "only one row per user" a DB-level guarantee
-- rather than an app-level convention; telegram_config_set() upserts
-- against it so adding again when one already exists replaces it instead
-- of erroring or creating a second row.
CREATE TABLE IF NOT EXISTS telegram_config (
    user_email TEXT PRIMARY KEY,
    bot_token  TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
"""


def _migrate(con):
    """Add columns to tables that existed before this field did. Idempotent:
    checks PRAGMA table_info first, never re-adds a column that's there.

    Also the one-time multi-user cutover: every table that used to be
    implicitly "the one candidate" (point_bank, skills, profile,
    applications, judge_progress, gap_clusters) gets keyed by user_email,
    and whatever existed before this ran is attributed to ADMIN_EMAIL --
    nothing is dropped, nothing silently becomes invisible."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
    if "content_hash" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN content_hash TEXT DEFAULT ''")
    if "location" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN location TEXT DEFAULT ''")
    if "posted_ts" not in cols:
        # the posting's OWN publish date, straight from the source ATS
        # (Greenhouse first_published / Lever createdAt / Ashby publishedAt)
        # -- distinct from fetched_ts (when we pulled it). Needed for the
        # fetch-time "max 1 month old" cutoff; a posting's updated_at isn't
        # safe for that since companies bump old listings without them being
        # new.
        con.execute("ALTER TABLE jobs ADD COLUMN posted_ts REAL")
    # llm_score/llm_reason/llm_judged_ts/llm_missing/llm_projects/
    # llm_requirements/notified_ts/status used to live as columns on jobs --
    # moved to job_user_state (one row per user per job) since scoring,
    # status, and notification state are now personal to each user, not a
    # property of the shared posting. A pre-existing DB still HAS these
    # columns (never dropped -- ALTER TABLE DROP COLUMN on a live prod SQLite
    # file is a real risk for zero functional benefit); they're just dead
    # weight now, backfilled into job_user_state once below, then ignored by
    # every function in this file going forward.
    had_old_scoring_cols = "llm_score" in cols

    cols = {r[1] for r in con.execute("PRAGMA table_info(point_bank)").fetchall()}
    if "entry_type" not in cols:
        # 'role' (work experience, under company/role/dates) or 'project' --
        # a project reuses the company column as its name and role as a
        # short tagline/stack line, same columns, no new ones needed.
        # Existing rows default to 'role', unaffected.
        con.execute("ALTER TABLE point_bank ADD COLUMN entry_type TEXT NOT NULL DEFAULT 'role'")
    if "user_email" not in cols:
        con.execute("ALTER TABLE point_bank ADD COLUMN user_email TEXT NOT NULL DEFAULT ''")
        con.execute("UPDATE point_bank SET user_email=? WHERE user_email=''", (ADMIN_EMAIL,))

    cols = {r[1] for r in con.execute("PRAGMA table_info(applications)").fetchall()}
    for name, ddl in [
        ("applied_via", "TEXT DEFAULT ''"),
        ("applied_ts", "REAL"),
        ("referral_name", "TEXT DEFAULT ''"),
        ("referral_note", "TEXT DEFAULT ''"),
        ("status", "TEXT NOT NULL DEFAULT 'generated'"),
        ("status_history", "TEXT DEFAULT '[]'"),
    ]:
        if name not in cols:
            con.execute(f"ALTER TABLE applications ADD COLUMN {name} {ddl}")
    if "user_email" not in cols:
        con.execute("ALTER TABLE applications ADD COLUMN user_email TEXT NOT NULL DEFAULT ''")
        con.execute("UPDATE applications SET user_email=? WHERE user_email=''", (ADMIN_EMAIL,))

    # skills/profile/judge_progress/gap_clusters all had a PRIMARY KEY that
    # can't just gain a column (name/key/id=1 alone used to BE the identity)
    # -- SQLite can't ALTER a primary key, so these are rename-copy-drop.
    cols = {r[1] for r in con.execute("PRAGMA table_info(skills)").fetchall()}
    if cols and "user_email" not in cols:
        con.execute("ALTER TABLE skills RENAME TO skills_old")
        con.execute("CREATE TABLE skills (user_email TEXT NOT NULL, name TEXT NOT NULL, "
                     "category TEXT NOT NULL, active INTEGER DEFAULT 1, PRIMARY KEY (user_email, name))")
        con.execute("INSERT INTO skills (user_email, name, category, active) "
                     "SELECT ?, name, category, active FROM skills_old", (ADMIN_EMAIL,))
        con.execute("DROP TABLE skills_old")

    cols = {r[1] for r in con.execute("PRAGMA table_info(profile)").fetchall()}
    if cols and "user_email" not in cols:
        con.execute("ALTER TABLE profile RENAME TO profile_old")
        con.execute("CREATE TABLE profile (user_email TEXT NOT NULL, key TEXT NOT NULL, "
                     "value TEXT NOT NULL, PRIMARY KEY (user_email, key))")
        con.execute("INSERT INTO profile (user_email, key, value) "
                     "SELECT ?, key, value FROM profile_old", (ADMIN_EMAIL,))
        con.execute("DROP TABLE profile_old")

    for table in ("judge_progress", "gap_clusters"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if cols and "user_email" not in cols:
            con.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            con.execute(f"CREATE TABLE {table} (user_email TEXT PRIMARY KEY, data TEXT NOT NULL)")
            con.execute(f"INSERT INTO {table} (user_email, data) SELECT ?, data FROM {table}_old WHERE id=1",
                        (ADMIN_EMAIL,))
            con.execute(f"DROP TABLE {table}_old")

    # One-time backfill: whatever scoring/status/notification state already
    # existed on jobs (pre-cutover) becomes ADMIN_EMAIL's own job_user_state
    # rows. Guarded on job_user_state being empty so this full scan only
    # ever runs once, not on every connect().
    if had_old_scoring_cols and con.execute("SELECT COUNT(*) FROM job_user_state").fetchone()[0] == 0:
        old_rows = con.execute(
            "SELECT id, status, llm_score, llm_reason, llm_judged_ts, llm_missing, "
            "llm_projects, llm_requirements, notified_ts FROM jobs "
            "WHERE status != 'new' OR llm_score IS NOT NULL OR notified_ts IS NOT NULL"
        ).fetchall()
        for r in old_rows:
            con.execute(
                "INSERT OR IGNORE INTO job_user_state (user_email, job_id, status, llm_score, "
                "llm_reason, llm_judged_ts, llm_missing, llm_projects, llm_requirements, notified_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ADMIN_EMAIL, r["id"], r["status"], r["llm_score"], r["llm_reason"], r["llm_judged_ts"],
                 r["llm_missing"], r["llm_projects"], r["llm_requirements"], r["notified_ts"]),
            )

    # Seed the admin -- the one email that was already the whole auth system
    # (ALLOWED_LOGIN_EMAIL) becomes role='admin' in the new whitelist table,
    # so the cutover to multi-user never locks out the person who was
    # already the only user.
    con.execute("INSERT OR IGNORE INTO users (email, role, added_ts) VALUES (?, 'admin', ?)",
                (ADMIN_EMAIL, time.time()))

    # jobs had ZERO indexes beyond the implicit rowid -- every url/
    # content_hash lookup (jobs_find_duplicate, called once per posting on
    # every single fetch run) was a full table scan. Measured live at
    # current size (7681 rows): 12ms/lookup without an index, 0.01ms with
    # one -- a real, compounding cost across hundreds of postings checked
    # per fetch, not a micro-optimization.
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_content_hash ON jobs(content_hash)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_job_user_state_user ON job_user_state(user_email)")
    con.commit()


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    # Real SQL-level filtering (a WHERE clause SQLite evaluates per row while
    # scanning), not a Python loop over every row after a bare SELECT * --
    # these call the EXACT same classifier functions ats.py already has and
    # already got right (word-boundary precision for seniority/location, the
    # "senior staff before staff" ordering), so there's no second
    # hand-translated copy of that logic to drift out of sync.
    con.create_function("IS_ENGINEERING_TITLE", 1,
                         lambda title: 1 if ats.is_likely_engineering_title(title or "") else 0)
    con.create_function("SENIORITY_TIER", 1, lambda title: ats.title_seniority_tier(title or ""))
    con.create_function("IS_TARGET_LOCATION", 2,
                         lambda loc, desc: 1 if ats.is_target_location(loc, desc) else 0)
    return con


# ── profile version (for "don't re-judge unless the master doc changed") ──
_PROFILE_VERSION_KEY = "_profile_version_ts"


def _touch_profile_version(user_email):
    """Called by every point_bank/skills/profile write. A job's llm_judged_ts
    older than THIS USER'S profile_version_ts means their own data changed
    since it was scored -- eligible for re-judging. Per-user: one person
    editing their resume must never mark another person's jobs stale. Raw
    SQL, not profile_set(), so this internal bookkeeping key never shows up
    mixed in with user-facing profile fields."""
    con = connect()
    con.execute(
        "INSERT INTO profile (user_email, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_email, key) DO UPDATE SET value=excluded.value",
        (user_email, _PROFILE_VERSION_KEY, str(time.time())),
    )
    con.commit()
    con.close()


def profile_version_ts(user_email):
    con = connect()
    row = con.execute("SELECT value FROM profile WHERE user_email=? AND key=?",
                       (user_email, _PROFILE_VERSION_KEY)).fetchone()
    con.close()
    return float(row["value"]) if row else 0.0


# ── point bank ──────────────────────────────────────────────────────────
def point_bank_add(user_email, company, role, start_date, end_date, text, tags=None,
                    metrics=None, source_note="", entry_type="role"):
    con = connect()
    cur = con.execute(
        "INSERT INTO point_bank (user_email, company, role, start_date, end_date, text, "
        "tags, metrics, source_note, entry_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_email, company, role, start_date, end_date, text,
         json.dumps(tags or []), json.dumps(metrics or []), source_note, entry_type),
    )
    con.commit()
    pid = cur.lastrowid
    con.close()
    _touch_profile_version(user_email)
    return pid


def point_bank_find_similar(user_email, text, threshold=0.82):
    """Best near-duplicate already in this user's point bank, or None.
    Catches the real case that kept happening: uploading a second resume
    variant re-extracts the same accomplishment reworded slightly, and it
    was getting added again with zero check. difflib.SequenceMatcher on
    normalized text (stdlib, no new dependency) -- not exact-match, since
    "Built X" and "Built and owned X" describing the same thing should
    still catch each other."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    best, best_ratio = None, 0.0
    for row in point_bank_all(user_email, active_only=False):
        existing_norm = re.sub(r"\s+", " ", row["text"].strip().lower())
        ratio = difflib.SequenceMatcher(None, norm, existing_norm).ratio()
        if ratio > best_ratio:
            best, best_ratio = row, ratio
    if best and best_ratio >= threshold:
        return best, best_ratio
    return None, 0.0


def point_bank_all(user_email, active_only=True):
    con = connect()
    q = "SELECT * FROM point_bank WHERE user_email=?"
    params = [user_email]
    if active_only:
        q += " AND active = 1"
    q += " ORDER BY start_date DESC, id"
    rows = [dict(r) for r in con.execute(q, params).fetchall()]
    con.close()
    for r in rows:
        r["tags"] = json.loads(r["tags"])
        r["metrics"] = json.loads(r["metrics"])
    return rows


def point_bank_set_active(user_email, pid, active):
    """Scoped by user_email in the WHERE clause, not just id -- a pid that
    belongs to someone else's point bank silently affects 0 rows rather
    than ever being editable cross-user."""
    con = connect()
    con.execute("UPDATE point_bank SET active=? WHERE id=? AND user_email=?",
                (1 if active else 0, pid, user_email))
    con.commit()
    con.close()
    _touch_profile_version(user_email)


def point_bank_update_text(user_email, pid, text):
    con = connect()
    con.execute("UPDATE point_bank SET text=? WHERE id=? AND user_email=?", (text, pid, user_email))
    con.commit()
    con.close()
    _touch_profile_version(user_email)


def point_bank_update_entry_type(user_email, pid, entry_type, role=None):
    """Reclassify a row between 'role' and 'project'. `role` optionally
    overwrites the role/tagline column in the same call -- needed when a
    row was filed as a fake 'role' (e.g. titled "Builder") for something
    that's really one project's tagline line, not a job title."""
    con = connect()
    if role is None:
        con.execute("UPDATE point_bank SET entry_type=? WHERE id=? AND user_email=?",
                    (entry_type, pid, user_email))
    else:
        con.execute("UPDATE point_bank SET entry_type=?, role=? WHERE id=? AND user_email=?",
                    (entry_type, role, pid, user_email))
    con.commit()
    con.close()
    _touch_profile_version(user_email)


# ── skills ──────────────────────────────────────────────────────────────
def skills_add(user_email, name, category):
    # Case-insensitive lookup -- the composite PK (user_email, name) is
    # case-sensitive, so "Embeddings" and "embeddings" used to insert as two
    # separate rows (found as real duplicate data: 6 skills double-counted
    # this way). Keeps whichever casing is already stored rather than
    # silently renaming it on a re-add with different casing.
    con = connect()
    existing = con.execute("SELECT name FROM skills WHERE user_email=? AND LOWER(name)=LOWER(?)",
                            (user_email, name)).fetchone()
    if existing:
        con.execute("UPDATE skills SET category=?, active=1 WHERE user_email=? AND name=?",
                    (category, user_email, existing["name"]))
    else:
        con.execute("INSERT INTO skills (user_email, name, category) VALUES (?,?,?)",
                    (user_email, name, category))
    con.commit()
    con.close()
    _touch_profile_version(user_email)


def skills_all(user_email, active_only=True):
    con = connect()
    q = "SELECT * FROM skills WHERE user_email=?"
    params = [user_email]
    if active_only:
        q += " AND active = 1"
    q += " ORDER BY category, name"
    rows = [dict(r) for r in con.execute(q, params).fetchall()]
    con.close()
    return rows


# ── profile (free-form key/value: name, email, links, summary variants) ──
def profile_set(user_email, key, value):
    con = connect()
    con.execute(
        "INSERT INTO profile (user_email, key, value) VALUES (?,?,?) "
        "ON CONFLICT(user_email, key) DO UPDATE SET value=excluded.value",
        (user_email, key, value),
    )
    con.commit()
    con.close()
    _touch_profile_version(user_email)


def profile_all(user_email):
    con = connect()
    rows = con.execute("SELECT key, value FROM profile WHERE user_email=?", (user_email,)).fetchall()
    con.close()
    return {r["key"]: r["value"] for r in rows if r["key"] != _PROFILE_VERSION_KEY}


# ── telegram_config (one bot/chat per user, keyed by login email) ───────
def telegram_config_get(user_email):
    con = connect()
    row = con.execute("SELECT * FROM telegram_config WHERE user_email=?", (user_email,)).fetchone()
    con.close()
    return dict(row) if row else None


def telegram_config_set(user_email, bot_token, chat_id):
    """Upsert -- a second call for the same user replaces their existing
    row rather than erroring or adding a second one (user_email is the
    PRIMARY KEY, so there can only ever be one)."""
    con = connect()
    con.execute(
        "INSERT INTO telegram_config (user_email, bot_token, chat_id, updated_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(user_email) DO UPDATE SET bot_token=excluded.bot_token, "
        "chat_id=excluded.chat_id, updated_ts=excluded.updated_ts",
        (user_email, bot_token, chat_id, time.time()),
    )
    con.commit()
    con.close()


# ── users (the admin whitelist -- who's allowed to actually use the app) ─
def users_all():
    con = connect()
    rows = con.execute("SELECT * FROM users ORDER BY role DESC, added_ts").fetchall()
    con.close()
    return [dict(r) for r in rows]


def users_get(email):
    con = connect()
    row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    con.close()
    return dict(row) if row else None


def users_add(email):
    """Whitelist one email -- always role='whitelisted'; the admin role is
    only ever seeded once, from ALLOWED_LOGIN_EMAIL (see _migrate), never
    grantable through this function. INSERT OR IGNORE: re-adding someone
    already on the list is a harmless no-op, not an error."""
    con = connect()
    con.execute("INSERT OR IGNORE INTO users (email, role, added_ts) VALUES (?, 'whitelisted', ?)",
                (email, time.time()))
    con.commit()
    con.close()


def users_remove(email):
    """Refuses to remove an admin -- the whitelist UI can only ever remove
    someone it also added (role='whitelisted'); there is no path to
    locking every admin out of their own app through this function."""
    con = connect()
    con.execute("DELETE FROM users WHERE email=? AND role != 'admin'", (email,))
    con.commit()
    con.close()


# ── jobs ────────────────────────────────────────────────────────────────
def _content_hash(company, title, description_raw):
    """Same posting reposted, or the same JD pasted twice, hashes the same
    regardless of whitespace/case differences. Not sensitive to url, since
    the same job is sometimes listed at more than one url."""
    norm = re.sub(r"\s+", " ", f"{company}|{title}|{description_raw}".strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def jobs_find_duplicate(company, title, url, description_raw):
    """Return the existing job row if this posting is already in the table,
    else None. Two ways to match: same url (exact), or same normalized
    company+title+description (catches the same JD pasted or fetched twice,
    even from different sources)."""
    con = connect()
    row = None
    if url:
        row = con.execute("SELECT * FROM jobs WHERE url=? AND url != ''", (url,)).fetchone()
    if row is None:
        h = _content_hash(company, title, description_raw)
        row = con.execute("SELECT * FROM jobs WHERE content_hash=?", (h,)).fetchone()
    con.close()
    return dict(row) if row else None


def jobs_backfill_description(jid, description_raw):
    """Overwrite an existing row's description_raw in place -- for repairing
    content a fetcher bug dropped the first time around (e.g. Lever's
    separate 'lists' field never being read), not the normal fetch path,
    which only ever inserts or leaves alone (see jobs_add). Recomputes
    content_hash to match the new text, and resets llm_judged_ts to 0 for
    EVERY user who's scored this job -- a content change invalidates
    everyone's score on this shared posting, not just one user's, and
    isn't caught by profile_version_ts (which only tracks changes to each
    candidate's own master doc, not to a job posting's JD)."""
    con = connect()
    row = con.execute("SELECT company, title FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        con.close()
        return
    h = _content_hash(row["company"], row["title"], description_raw)
    con.execute("UPDATE jobs SET description_raw=?, content_hash=? WHERE id=?",
                (description_raw, h, jid))
    con.execute("UPDATE job_user_state SET llm_judged_ts=0 WHERE job_id=?", (jid,))
    con.commit()
    con.close()


def jobs_add(source, company, title, url, description_raw, location="", posted_ts=None):
    """Returns (job_id, is_new). is_new=False means this exact posting was
    already in the table -- the existing row's id comes back, nothing new
    is inserted, so callers can't silently double-track the same job.

    If the existing row has no location on file yet and this call has one,
    backfills it in place -- lets a plain re-run of the fetch fill in
    location for postings that were tracked before location was captured
    at all, with no separate migration script needed."""
    dup = jobs_find_duplicate(company, title, url, description_raw)
    if dup:
        if location and not dup.get("location"):
            con = connect()
            con.execute("UPDATE jobs SET location=? WHERE id=?", (location, dup["id"]))
            con.commit()
            con.close()
        return dup["id"], False

    con = connect()
    h = _content_hash(company, title, description_raw)
    cur = con.execute(
        "INSERT INTO jobs (source, company, title, url, description_raw, fetched_ts, content_hash, location, posted_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (source, company, title, url, description_raw, time.time(), h, location, posted_ts),
    )
    con.commit()
    jid = cur.lastrowid
    con.close()
    return jid, True


# Every per-user scoring/status/notification field, joined onto the shared
# jobs row for ONE user -- the same dict shape callers used when these were
# plain columns on jobs, so jobs_list/jobs_query/jobs_get callers elsewhere
# don't need to change how they read a job dict, only how they ask for one.
_JOB_USER_JOIN = (
    "LEFT JOIN job_user_state s ON s.job_id = j.id AND s.user_email = ? "
)
_JOB_USER_COLS = (
    "COALESCE(s.status, 'new') AS status, s.llm_score AS llm_score, "
    "s.llm_reason AS llm_reason, s.llm_judged_ts AS llm_judged_ts, "
    "s.llm_missing AS llm_missing, s.llm_projects AS llm_projects, "
    "s.llm_requirements AS llm_requirements, s.notified_ts AS notified_ts"
)


def jobs_list(user_email, status=None):
    con = connect()
    q = f"SELECT j.*, {_JOB_USER_COLS} FROM jobs j {_JOB_USER_JOIN}"
    params = [user_email]
    if status:
        q += "WHERE COALESCE(s.status, 'new') = ? "
        params.append(status)
    q += "ORDER BY s.llm_score DESC NULLS LAST, j.fetched_ts DESC"
    rows = con.execute(q, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


def jobs_query(user_email, status=None, eng_only=True, level_tiers=None, min_score=None, q_text="",
               company_filter="", source_filter="", date_cutoff=None, exclude_applied=True):
    """The real filter for the Postings list -- one SQL query with a WHERE
    clause SQLite evaluates per row while scanning, not a SELECT * pulled
    into Python followed by a manual loop over every row (what this
    replaced). IS_ENGINEERING_TITLE/SENIORITY_TIER/IS_TARGET_LOCATION are
    registered on the connection in connect() -- same classifier functions
    ats.py already has, called per-row by SQLite itself, so there's no
    second hand-translated copy of that logic (word-boundary precision,
    "senior staff" before "staff" ordering) to drift out of sync.

    India/Bangalore/Bengaluru location is unconditional, not a parameter --
    this whole app only ever targets that location now.

    status/llm_score/notified/etc all come from job_user_state scoped to
    user_email -- two users looking at the same shared posting never see
    each other's score or status.

    Returns (total_unfiltered, matched_jobs) -- matched_jobs already sorted
    by fetch date descending (newest first), score as the tiebreaker among
    postings fetched at the same time."""
    con = connect()

    unfiltered_q, unfiltered_params = (
        f"SELECT COUNT(*) FROM jobs j {_JOB_USER_JOIN}", [user_email])
    if status:
        unfiltered_q += "WHERE COALESCE(s.status, 'new') = ?"
        unfiltered_params.append(status)
    total_unfiltered = con.execute(unfiltered_q, unfiltered_params).fetchone()[0]

    # Manual jobs bypass the India-only location filter -- found live: a job added
    # by hand via manage.py/the "Add job manually" dialog with no India location
    # (and no India mention in the JD's first 500 chars) was silently invisible in
    # the UI, even with the "Manual only" source filter selected, because this
    # location check is unconditional. The filter exists to de-clutter the
    # AUTOMATED feed (it discards ~11k US/EU postings per cron fetch); a job the
    # user deliberately added by hand is wanted by definition and should always
    # show up.
    where = ["(j.source = 'manual' OR IS_TARGET_LOCATION(j.location, j.description_raw) = 1)"]
    params = [user_email]
    if status:
        where.append("COALESCE(s.status, 'new') = ?")
        params.append(status)
    if eng_only:
        where.append("IS_ENGINEERING_TITLE(j.title) = 1")
    if level_tiers:
        where.append(f"SENIORITY_TIER(j.title) IN ({','.join('?' * len(level_tiers))})")
        params.extend(level_tiers)
    if min_score is not None:
        # NULL llm_score (not yet AI-judged) always passes -- "llm_score >= ?"
        # alone would silently drop every unjudged posting, since SQL compares
        # against NULL as false, which is the opposite of what a min-score
        # filter should do before judging has even run.
        where.append("(s.llm_score IS NULL OR s.llm_score >= ?)")
        params.append(float(min_score))
    if q_text:
        where.append("(LOWER(j.company) LIKE ? OR LOWER(j.title) LIKE ?)")
        like = f"%{q_text}%"
        params.extend([like, like])
    if company_filter:
        where.append("LOWER(j.company) = ?")
        params.append(company_filter)
    if source_filter == "manual":
        where.append("j.source = 'manual'")
    elif source_filter == "auto":
        where.append("j.source != 'manual'")
    if date_cutoff is not None:
        where.append("j.fetched_ts >= ?")
        params.append(date_cutoff)
    if exclude_applied:
        where.append("j.id NOT IN (SELECT DISTINCT job_id FROM applications "
                      "WHERE status != 'generated' AND user_email = ?)")
        params.append(user_email)

    query = (f"SELECT j.*, {_JOB_USER_COLS} FROM jobs j {_JOB_USER_JOIN}WHERE " + " AND ".join(where) +
              " ORDER BY j.fetched_ts DESC, s.llm_score DESC NULLS LAST")
    rows = con.execute(query, params).fetchall()
    con.close()
    return total_unfiltered, [dict(r) for r in rows]


def jobs_get(user_email, jid):
    con = connect()
    row = con.execute(
        f"SELECT j.*, {_JOB_USER_COLS} FROM jobs j {_JOB_USER_JOIN}WHERE j.id=?",
        (user_email, jid),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def jobs_added_count(start_ts, end_ts=None):
    """Count of postings first added (fetched_ts) in [start_ts, end_ts),
    or [start_ts, now) if end_ts is omitted."""
    con = connect()
    if end_ts is None:
        row = con.execute("SELECT COUNT(*) FROM jobs WHERE fetched_ts >= ?", (start_ts,)).fetchone()
    else:
        row = con.execute("SELECT COUNT(*) FROM jobs WHERE fetched_ts >= ? AND fetched_ts < ?",
                           (start_ts, end_ts)).fetchone()
    con.close()
    return row[0]


def jobs_ids():
    """Every job id in the shared postings table -- no per-user join, for
    callers (the fetch before/after diff) that only need to know which
    ids exist, not anyone's personalized view of them."""
    con = connect()
    rows = con.execute("SELECT id FROM jobs").fetchall()
    con.close()
    return {r["id"] for r in rows}




def _ensure_job_user_row(con, user_email, jid):
    """INSERT OR IGNORE -- makes sure a (user_email, job_id) row exists so a
    plain UPDATE below has something to touch, without ever overwriting
    columns an UPDATE isn't setting."""
    con.execute("INSERT OR IGNORE INTO job_user_state (user_email, job_id) VALUES (?,?)",
                (user_email, jid))


def jobs_set_llm_score(user_email, jid, score, reason, missing=None, projects=None, requirements=None):
    con = connect()
    con.execute(
        "INSERT INTO job_user_state (user_email, job_id, llm_score, llm_reason, llm_judged_ts, "
        "llm_missing, llm_projects, llm_requirements) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_email, job_id) DO UPDATE SET llm_score=excluded.llm_score, "
        "llm_reason=excluded.llm_reason, llm_judged_ts=excluded.llm_judged_ts, "
        "llm_missing=excluded.llm_missing, llm_projects=excluded.llm_projects, "
        "llm_requirements=excluded.llm_requirements",
        (user_email, jid, score, reason, time.time(), json.dumps(missing or []),
         json.dumps(projects or []), json.dumps(requirements or [])),
    )
    con.commit()
    con.close()


def jobs_mark_notified(user_email, jid):
    """Called once the poll loop has actually sent a crossed-threshold
    notification for this job to this user -- the gate that stops every
    later 10-minute tick from re-sending it to them."""
    con = connect()
    _ensure_job_user_row(con, user_email, jid)
    con.execute("UPDATE job_user_state SET notified_ts=? WHERE user_email=? AND job_id=?",
                (time.time(), user_email, jid))
    con.commit()
    con.close()


def jobs_clear_notified(user_email, jid):
    """Called when a job's score drops back below the notify threshold, so
    a later genuine re-crossing notifies this user again instead of being
    silently swallowed forever by the first notification."""
    con = connect()
    con.execute("UPDATE job_user_state SET notified_ts=NULL WHERE user_email=? AND job_id=?",
                (user_email, jid))
    con.commit()
    con.close()


def jobs_llm_score_stale(user_email, job):
    """True if this posting has never been judged FOR THIS USER, or was
    judged before this user's own point bank/skills/profile last changed --
    the "don't re-judge unless the master doc changed" rule. A judged
    posting whose score predates the latest profile edit is treated the
    same as an unjudged one; everything else is left alone."""
    if job["llm_score"] is None:
        return True
    judged_ts = job["llm_judged_ts"] or 0
    return judged_ts < profile_version_ts(user_email)


def jobs_clear_llm_scores(user_email, date_cutoff=None):
    """Wipe this user's llm_score/llm_reason/llm_judged_ts -- the explicit
    "force reload" the UI offers, for when their profile changed enough
    that they want a clean re-judge pass rather than relying on the
    staleness check alone. date_cutoff (optional, a fetched_ts): only
    clears postings fetched at or after it, so clearing doesn't also make
    a posting from months back newly eligible for a real LLM spend on the
    next judge pass.

    Also clears notified_ts -- found live: clearing a score and re-judging
    it via the AI-judge button never re-notified, even when the fresh
    score crossed the threshold again, because notified_ts was never
    touched here. A "start over" action on a job's score should mean a
    genuinely fresh notification is possible too, not just a fresh number
    nobody ever hears about -- the AI-judge button itself never sends
    notifications (only poll_and_notify's own cron tick does), so this is
    the one place that needs to put the job back in "eligible to notify"
    state for that next tick to actually pick it up.

    Returns how many rows were actually cleared."""
    con = connect()
    q = ("UPDATE job_user_state SET llm_score=NULL, llm_reason='', llm_judged_ts=NULL, "
         "llm_missing='[]', llm_projects='[]', llm_requirements='[]', notified_ts=NULL "
         "WHERE user_email=? AND llm_score IS NOT NULL")
    params = [user_email]
    if date_cutoff is not None:
        q += " AND job_id IN (SELECT id FROM jobs WHERE fetched_ts >= ?)"
        params.append(date_cutoff)
    cur = con.execute(q, params)
    con.commit()
    n = cur.rowcount
    con.close()
    return n


def jobs_set_status(user_email, jid, status):
    con = connect()
    con.execute(
        "INSERT INTO job_user_state (user_email, job_id, status) VALUES (?,?,?) "
        "ON CONFLICT(user_email, job_id) DO UPDATE SET status=excluded.status",
        (user_email, jid, status),
    )
    con.commit()
    con.close()


def judge_progress_get(user_email):
    """This user's last known AI-judge batch state, or None if they've
    never run one. Survives a server restart -- this is disk, not the
    process's own memory."""
    con = connect()
    row = con.execute("SELECT data FROM judge_progress WHERE user_email=?", (user_email,)).fetchone()
    con.close()
    return json.loads(row["data"]) if row else None


def judge_progress_set(user_email, data):
    """Overwrites this user's one current/last-known state -- called on
    every real change (batch start, each job finishing, batch end), not
    just at the end, so a crash or restart mid-batch still leaves the
    real, honest progress on disk instead of nothing."""
    con = connect()
    con.execute(
        "INSERT INTO judge_progress (user_email, data) VALUES (?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET data = excluded.data",
        (user_email, json.dumps(data)),
    )
    con.commit()
    con.close()


def gap_clusters_get(user_email):
    """This user's last computed Gaps-tab result, or None if it's never
    been run."""
    con = connect()
    row = con.execute("SELECT data FROM gap_clusters WHERE user_email=?", (user_email,)).fetchone()
    con.close()
    return json.loads(row["data"]) if row else None


def gap_clusters_set(user_email, data):
    """Overwrites this user's one cached result -- called only when the
    Gaps tab actually recomputes (first-ever load, or an explicit
    Refresh), never on a plain tab-open."""
    con = connect()
    con.execute(
        "INSERT INTO gap_clusters (user_email, data) VALUES (?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET data = excluded.data",
        (user_email, json.dumps(data)),
    )
    con.commit()
    con.close()


# ── applications ────────────────────────────────────────────────────────
def applications_add(user_email, job_id, resume_path, cover_letter_path, ats_estimate, folder_path):
    con = connect()
    cur = con.execute(
        "INSERT INTO applications (user_email, job_id, resume_path, cover_letter_path, "
        "ats_estimate, generated_ts, folder_path) VALUES (?,?,?,?,?,?,?)",
        (user_email, job_id, resume_path, cover_letter_path, json.dumps(ats_estimate),
         time.time(), folder_path),
    )
    con.commit()
    aid = cur.lastrowid
    con.close()
    return aid


def applications_find_draft(user_email, job_id):
    """This user's most recent application for this job still at
    status='generated' -- materials exist but nothing was actually
    submitted yet. Used before a regenerate so a second draft replaces the
    first instead of leaving two rows for the same job (status !=
    'generated' -- a real submitted application -- is never touched this
    way; that's a real record, not a draft to overwrite)."""
    con = connect()
    row = con.execute(
        "SELECT * FROM applications WHERE job_id=? AND user_email=? AND status='generated' "
        "ORDER BY generated_ts DESC LIMIT 1", (job_id, user_email)
    ).fetchone()
    con.close()
    return dict(row) if row else None


def applications_delete(user_email, app_id):
    """Scoped by user_email -- an app_id belonging to someone else's
    application silently affects 0 rows, never deletable cross-user."""
    con = connect()
    con.execute("DELETE FROM applications WHERE id=? AND user_email=?", (app_id, user_email))
    con.commit()
    con.close()


def applications_for_job(user_email, job_id):
    """Only THIS user's applications for the shared job -- another user
    may have generated their own resume/cover letter for the same posting,
    and that must never show up here."""
    con = connect()
    rows = con.execute(
        "SELECT * FROM applications WHERE job_id=? AND user_email=? ORDER BY generated_ts DESC",
        (job_id, user_email),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ats_estimate"] = json.loads(d["ats_estimate"])
        d["status_history"] = json.loads(d["status_history"])
        out.append(d)
    return out


def _application_row(con, user_email, app_id):
    row = con.execute("SELECT * FROM applications WHERE id=? AND user_email=?",
                       (app_id, user_email)).fetchone()
    if row is None:
        raise ValueError(f"no application with id={app_id} for this user")
    return row


def applications_already_applied(user_email, job_id):
    """True if any of THIS user's applications for this job has moved past
    'generated' -- i.e. they actually submitted at some point. The dedup
    check before marking applied again, or before regenerating without
    noticing. Another user's applied status for the same shared job is
    irrelevant here -- each person's own application funnel is their own."""
    con = connect()
    row = con.execute(
        "SELECT 1 FROM applications WHERE job_id=? AND user_email=? AND status != 'generated' LIMIT 1",
        (job_id, user_email),
    ).fetchone()
    con.close()
    return row is not None


def applications_all_applied_job_ids(user_email):
    """Every job_id THIS user has at least one application past
    'generated' for -- one query for the whole set, so job-list filtering
    doesn't run applications_already_applied() once per row."""
    con = connect()
    rows = con.execute(
        "SELECT DISTINCT job_id FROM applications WHERE status != 'generated' AND user_email=?",
        (user_email,),
    ).fetchall()
    con.close()
    return {r["job_id"] for r in rows}


def applications_mark_applied(user_email, app_id, applied_via, referral_name="", referral_note=""):
    """Record the real-world moment of submitting. applied_via: company_site|
    linkedin|referral|recruiter|other."""
    con = connect()
    row = _application_row(con, user_email, app_id)
    hist = json.loads(row["status_history"])
    hist.append({"status": "applied", "ts": time.time(), "note": f"via {applied_via}"})
    con.execute(
        "UPDATE applications SET applied_via=?, applied_ts=?, referral_name=?, "
        "referral_note=?, status='applied', status_history=? WHERE id=? AND user_email=?",
        (applied_via, time.time(), referral_name, referral_note, json.dumps(hist), app_id, user_email),
    )
    con.commit()
    con.close()


def applications_quick_mark_applied(user_email, job_id, applied_via, referral_name="", referral_note=""):
    """Mark a job applied WITHOUT going through the generate pipeline --
    for the real, common case of applying directly on the company's site or
    LinkedIn, with your own resume, never touching this tool's drafting
    flow at all. Creates a real applications row (empty resume/cover-letter/
    folder_path -- there are no generated materials for this one) with
    status='applied' from the start, so it's picked up by the exact same
    exclude-from-postings and Applications-tab logic a generated-then-marked
    application already uses -- no second, parallel "applied" concept to
    keep in sync with this one."""
    con = connect()
    now = time.time()
    hist = [{"status": "applied", "ts": now, "note": f"via {applied_via}"}]
    cur = con.execute(
        "INSERT INTO applications (user_email, job_id, resume_path, cover_letter_path, ats_estimate, "
        "generated_ts, folder_path, applied_via, applied_ts, referral_name, referral_note, "
        "status, status_history) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_email, job_id, "", "", "{}", now, "", applied_via, now, referral_name, referral_note,
         "applied", json.dumps(hist)),
    )
    con.commit()
    aid = cur.lastrowid
    con.close()
    return aid


def applications_add_status_event(user_email, app_id, status, note=""):
    """Append a stage to the funnel: phone_screen, interview, offer,
    rejected, ghosted, withdrawn. Keeps the full history, not just the
    latest stage, so 'what actually happened' is never lost."""
    con = connect()
    row = _application_row(con, user_email, app_id)
    hist = json.loads(row["status_history"])
    hist.append({"status": status, "ts": time.time(), "note": note})
    con.execute(
        "UPDATE applications SET status=?, status_history=? WHERE id=? AND user_email=?",
        (status, json.dumps(hist), app_id, user_email),
    )
    con.commit()
    con.close()


def applications_all(user_email):
    con = connect()
    rows = con.execute(
        "SELECT a.*, j.company, j.title FROM applications a "
        "JOIN jobs j ON j.id = a.job_id WHERE a.user_email=? ORDER BY a.generated_ts DESC",
        (user_email,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        d["ats_estimate"] = json.loads(d["ats_estimate"])
        d["status_history"] = json.loads(d["status_history"])
        out.append(d)
    return out
