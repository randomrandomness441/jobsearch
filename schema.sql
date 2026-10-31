-- Relational schema for the job application system. No JSON columns: every
-- list is a child table, every state is a CHECKed column, every owner is a
-- foreign key. connect() in db.py runs the three PRAGMAs below on every
-- connection (foreign_keys is per-connection in SQLite, off by default).
--
-- Flow: fetch inserts into jobs (deduped on company + ATS listing id) and
-- inserts one analysis row per eligible user, status 'pending'. The worker
-- claims a row (pending|retry -> in_process), runs score -> select -> draft
-- -> render, then flips it to done. Failure goes to retry until the 3rd
-- attempt, then failed. The (user_id, job_id) primary key is the whole
-- dedup story: the same posting pulled again is an INSERT OR IGNORE no-op,
-- so it can never be queued twice.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,      -- never reused: a master document file is named by it
    email    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role     TEXT NOT NULL DEFAULT 'whitelisted' CHECK (role IN ('admin', 'whitelisted')),
    added_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_config (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    bot_token  TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

-- The user's master document (profile, skills, point bank) is NOT in this
-- database: it is one JSON file per user, data/master/<user_id>.json, written
-- only by the user (master.py). Analysis rows below refer to its points by id.

-- Shared by every user: one row per posting.
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('greenhouse', 'lever', 'ashby', 'manual')),
    external_id     TEXT NOT NULL,              -- the ATS's own listing id; a content hash for manual jobs
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL DEFAULT '',
    description_raw TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',
    posted_ts       REAL,                       -- the posting's own publish date, from the ATS
    first_seen_ts   REAL NOT NULL,              -- when we first pulled it
    last_seen_ts    REAL NOT NULL,              -- bumped every time a fetch sees it again
    UNIQUE (company, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_ts);

-- One user's work item for one posting. Doubles as the queue's source of truth.
CREATE TABLE IF NOT EXISTS analysis (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id        INTEGER NOT NULL REFERENCES jobs(id)  ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'in_process', 'retry', 'done', 'failed', 'cleared')),
    attempts      INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),   -- counted at claim
    error_kind    TEXT CHECK (error_kind IN ('timeout', 'llm', 'render', 'invalid', 'interrupted', 'internal')),
    error_msg     TEXT,
    created_ts    REAL NOT NULL,
    queued_ts     REAL,                         -- claimed: pushed onto the in-memory queue
    started_ts    REAL,                         -- the worker began processing it
    finished_ts   REAL,
    score         INTEGER CHECK (score BETWEEN 0 AND 100),
    reason        TEXT,
    provider      TEXT,                         -- which LLM actually answered
    resume_path   TEXT,
    cover_path    TEXT,
    PRIMARY KEY (user_id, job_id),
    -- done means the files exist; failed means we can say why.
    CHECK (status <> 'done'   OR (resume_path IS NOT NULL AND cover_path IS NOT NULL AND finished_ts IS NOT NULL)),
    CHECK (status <> 'failed' OR (error_kind IS NOT NULL AND finished_ts IS NOT NULL))
);
-- What the poller scans for: pending|retry, oldest first.
CREATE INDEX IF NOT EXISTS idx_analysis_status ON analysis(status, created_ts);

CREATE TABLE IF NOT EXISTS analysis_requirement (
    user_id           INTEGER NOT NULL,
    job_id            INTEGER NOT NULL,
    pos               INTEGER NOT NULL,
    requirement       TEXT NOT NULL,
    importance        TEXT NOT NULL CHECK (importance IN ('required', 'nice_to_have')),
    presence          TEXT NOT NULL CHECK (presence IN ('yes', 'semantic', 'no')),
    depth             TEXT NOT NULL CHECK (depth IN ('quantified', 'named', 'n/a')),
    evidence_point_id INTEGER,                   -- a point id in the user's master document, or NULL
    weight            INTEGER NOT NULL CHECK (weight BETWEEN 0 AND 100),
    earned            INTEGER NOT NULL CHECK (earned >= 0 AND earned <= weight),
    PRIMARY KEY (user_id, job_id, pos),
    FOREIGN KEY (user_id, job_id) REFERENCES analysis(user_id, job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS analysis_gap (
    user_id INTEGER NOT NULL,
    job_id  INTEGER NOT NULL,
    pos     INTEGER NOT NULL,
    gap     TEXT NOT NULL,
    why     TEXT NOT NULL DEFAULT '',
    example TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, job_id, pos),
    FOREIGN KEY (user_id, job_id) REFERENCES analysis(user_id, job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS analysis_project (
    user_id         INTEGER NOT NULL,
    job_id          INTEGER NOT NULL,
    point_id        INTEGER NOT NULL,            -- a point id in the user's master document
    name            TEXT NOT NULL,               -- the project's name, as it was when assessed
    worth_including INTEGER NOT NULL CHECK (worth_including IN (0, 1)),
    reason          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, job_id, point_id),
    FOREIGN KEY (user_id, job_id) REFERENCES analysis(user_id, job_id) ON DELETE CASCADE
);

-- The bullets that actually went on the generated resume, in order.
CREATE TABLE IF NOT EXISTS analysis_item (
    user_id  INTEGER NOT NULL,
    job_id   INTEGER NOT NULL,
    pos      INTEGER NOT NULL,
    point_id INTEGER NOT NULL,                   -- a point id in the user's master document
    kind     TEXT NOT NULL CHECK (kind IN ('role', 'project')),
    text     TEXT NOT NULL,                     -- possibly a truthful reword of the point's own text
    PRIMARY KEY (user_id, job_id, pos),
    FOREIGN KEY (user_id, job_id) REFERENCES analysis(user_id, job_id) ON DELETE CASCADE
);

-- Timing and token metrics: one row per pipeline step per attempt.
CREATE TABLE IF NOT EXISTS analysis_step (
    user_id           INTEGER NOT NULL,
    job_id            INTEGER NOT NULL,
    attempt           INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 3),
    step              TEXT NOT NULL CHECK (step IN ('score', 'select', 'draft', 'render')),
    started_ts        REAL NOT NULL,
    duration_ms       INTEGER NOT NULL,
    provider          TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens  INTEGER,
    cached_tokens     INTEGER,
    ok                INTEGER NOT NULL CHECK (ok IN (0, 1)),
    error             TEXT,
    PRIMARY KEY (user_id, job_id, attempt, step),
    FOREIGN KEY (user_id, job_id) REFERENCES analysis(user_id, job_id) ON DELETE CASCADE
);

-- The user's own application funnel. A job is applied to at most once.
CREATE TABLE IF NOT EXISTS application (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id        INTEGER NOT NULL REFERENCES jobs(id)  ON DELETE CASCADE,
    applied_via   TEXT NOT NULL DEFAULT 'other'
                  CHECK (applied_via IN ('company_site', 'linkedin', 'referral', 'recruiter', 'other')),
    applied_ts    REAL NOT NULL,
    referral_name TEXT NOT NULL DEFAULT '',
    referral_note TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'applied'
                  CHECK (status IN ('applied', 'phone_screen', 'interview', 'offer', 'rejected', 'ghosted', 'withdrawn')),
    UNIQUE (user_id, job_id)
);

CREATE TABLE IF NOT EXISTS application_event (
    application_id INTEGER NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    ts             REAL NOT NULL,
    status         TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (application_id, seq)
);

-- Gaps tab: clusters of similar gaps across a user's postings.
CREATE TABLE IF NOT EXISTS gap_cluster (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    computed_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gap_cluster_job (
    cluster_id INTEGER NOT NULL REFERENCES gap_cluster(id) ON DELETE CASCADE,
    job_id     INTEGER NOT NULL REFERENCES jobs(id)        ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, job_id)
);
