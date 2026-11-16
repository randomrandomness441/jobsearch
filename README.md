# Lupin

A personal job-application pipeline. It pulls postings from Greenhouse, Lever and
Ashby boards, keeps only the ones worth applying to, and for each one generates a
tailored resume and cover letter (PDF + DOCX) from a master document that only you
edit. Python standard library, SQLite and vanilla JS; the one third-party
dependency is `python-docx`.

## How it works

```
fetch (every 15 min) ──► jobs table ──► analysis row per (user, job): pending
                                              │
                         one sequential worker claims rows (manual before auto)
                                              ▼
                  score requirements ─► select evidence ─► draft resume + cover letter
                                              │
                         render docx ─► pdf (LibreOffice) ─► Telegram notification
```

- **Fetch-time filtering.** All validation happens when postings are fetched, once:
  location is remote / Bengaluru / Bangalore; the title must be software engineer
  (any level), backend engineer, data engineer, SDE 1-3, member of technical staff
  or MTS; manager/director/VP and intern/co-op/forward-deployed/applied-AI titles
  are dropped; postings older than 30 days are dropped. Years of experience in the
  description are ignored. See `ats.py` and `fetch_ats.py`.
- **One shared `jobs` table, one `analysis` row per (user, job).** Status goes
  `pending → in_process → done | failed`, with retry and clear. A unique constraint
  and an atomic conditional `UPDATE` claim mean a listing is never processed twice.
- **Strictly sequential worker.** A single thread fills an in-memory priority queue
  from the database (manual requests before automatic ones) and calls
  `pipeline.process_one` for each row; the manual path uses the same function.
  Limit of 4 minutes per listing, up to 3 attempts, failure reason (e.g. `timeout`)
  stored on the row, start/end times and metrics saved in the database.
- **Three LLM calls per posting:** score the requirements, select supporting points,
  draft the documents. A gaps view clusters the requirements you could not evidence.
- **Master document** is `data/master/<user_id>.json`, written only by you (Profile
  tab or by hand). No LLM ever touches it. Point ids are stable; analysis rows refer
  to them.
- **Files** live at `data/files/<user>/<company>/<posting_id>/`. Download links can
  be signed and expire after 7 days.
- **Login** is Google OAuth with a signed session cookie. Logged-in users who are not
  on the allow-list are guests and can only browse shared postings.

## Layout

| File | Role |
| --- | --- |
| `schema.sql`, `db.py` | tables, transactions, queue claim, recovery of interrupted rows |
| `ats.py`, `fetch_ats.py`, `companies.py` | title/location classifiers, board fetchers, company list |
| `worker.py` | the single worker thread and the fetch thread |
| `pipeline.py`, `llm.py` | the three-step generation, time budget, retries, LLM client |
| `master.py` | per-user master document (JSON, atomic writes) |
| `resume_render.py`, `generate.py` | docx building and PDF conversion |
| `server.py`, `auth.py`, `links.py` | HTTP API, login, signed links |
| `static/` | the single-page UI |

## Running

Requirements: Python 3.10+, LibreOffice (`soffice`) for PDFs, `pip install -r requirements.txt`.

```
export SESSION_SECRET=...            # signs session cookies (or GENERATE_LINK_SECRET)
export ZAI_API_KEY=...               # LLM
export GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=...
export ALLOWED_LOGIN_EMAIL=you@example.com     # the admin
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...   # optional
python3 server.py                    # http://localhost:8777
```

Data goes to `data/` (override with `JOBSEARCH_DATA`). Nothing in it is committed.

## Tests

```
python3 run_tests.py          # everything
python3 run_tests.py --fast   # skips the ~40s real-PDF render test
```

The suites use a throwaway database and data directory, a fake LLM and a real HTTP
server on an ephemeral port. They check behavior, so removing a feature makes its
tests fail.

| Suite | What it pins down |
| --- | --- |
| `test_ats.py` | one generated test per title case: accepted, rejected, manager tier, location |
| `test_fetch.py` | what gets stored, what is skipped and counted, age limit, dedupe |
| `test_db.py` | state machine, claim atomicity, retries, restart recovery, cascades |
| `test_master.py` | validation, atomic writes, stable point ids |
| `test_worker.py` | priority order, refill, nothing processed twice |
| `test_pipeline.py` | budget, retries, error kinds, real docx → pdf render |
| `test_server.py` | auth, guests, downloads and signed links, path containment, every route |
| `test_jobparse.js` | the paste-a-posting title/location guesser |
