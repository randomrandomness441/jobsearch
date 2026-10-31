"""The background half of the app: one worker thread, one fetch thread.

    fetch thread    every FETCH_EVERY_SECONDS (or on demand): pull the ATS
                    boards, then give every ready user a pending analysis row
                    for each new eligible posting.
    worker thread   strictly one job at a time -- the LLM has no concurrency
                    to spare. When its in-memory queue is empty it asks the
                    database for pending/retry rows, claims them (-> in_process)
                    and queues them; then it pops, runs pipeline.process_one,
                    and notifies.

Manual jobs (a posting someone added, or a Regenerate click) go straight into
the same queue at priority 0, ahead of the poller's priority 1, but never
interrupt the job already running.

The database is the source of truth and the queue is only a cache of it: a row
reaches the queue only by winning db.analysis_claim, so the same job can't be
queued twice however it arrives, and a restart rebuilds everything from the
rows (db.analysis_reset_interrupted).
"""

import itertools
import os
import queue
import threading
import time
import traceback

import db
import fetch_ats
import links
import notify_telegram
import pipeline

BATCH = 20                       # rows claimed per refill
IDLE_SECONDS = 5                 # how long the worker waits on an empty queue before polling the DB again
FETCH_EVERY_SECONDS = 15 * 60
NEW_ROW_WINDOW_DAYS = 14         # only postings first seen this recently get rows created for a user
MANUAL, AUTO = 0, 1              # queue priorities; lower runs first

_queue = queue.PriorityQueue()
_seq = itertools.count()         # tie-breaker: first in, first out within a priority
_current = None                  # (user_id, job_id) the worker is running right now
_fetch_wanted = threading.Event()
fetch_state = {"running": False, "started_ts": None, "finished_ts": None, "ok": None,
               "new": 0, "seen": 0, "errors": 0, "rows_created": 0, "error": ""}


def enqueue(user_id, job_id, manual):
    """Claim the row and queue it. Returns False if someone already holds it
    (queued, running, or already finished) -- nothing is queued twice."""
    if not db.analysis_claim(user_id, job_id):
        return False
    _queue.put((MANUAL if manual else AUTO, next(_seq), user_id, job_id))
    return True


def status():
    return {"queued": _queue.qsize(), "current": list(_current) if _current else None, "fetch": dict(fetch_state)}


def _notify(user_id, job_id, status):
    """Telegram, for postings the poller found (a manual job's owner is
    already looking at the page). Only for a finished or finally-failed job."""
    job = db.jobs_get(job_id)
    cfg = db.telegram_config_get(user_id)
    if job is None or cfg is None or job["source"] == "manual" or status not in ("done", "failed"):
        return
    if status == "done":
        base = os.environ.get("JOBSEARCH_BASE_URL", "")
        notify_telegram.send_generation_complete(
            job, links.download_url(base, user_id, job_id, "resume"), links.download_url(base, user_id, job_id, "cover"),
            bot_token=cfg["bot_token"], chat_id=cfg["chat_id"])
    else:
        a = db.analysis_get(user_id, job_id)
        notify_telegram.send_generation_failed(job, a["error_msg"], bot_token=cfg["bot_token"], chat_id=cfg["chat_id"])


def run_once(wait=IDLE_SECONDS):
    """One worker iteration. If the queue is empty, refill it from the
    database first. Returns True if a job was processed."""
    global _current
    if _queue.empty():
        for row in db.analysis_pending(BATCH):
            enqueue(row["user_id"], row["job_id"], bool(row["manual"]))
    try:
        _, _, user_id, job_id = _queue.get(timeout=wait)
    except queue.Empty:
        return False
    _current = (user_id, job_id)
    try:
        started = time.time()
        print(f"[worker] start  user={user_id} job={job_id} attempt={db.analysis_attempt(user_id, job_id)} "
              f"queue_left={_queue.qsize()}", flush=True)
        try:
            status = pipeline.process_one(user_id, job_id)
        except Exception as e:      # process_one handles its own errors; this is a DB-level failure
            traceback.print_exc()
            status = db.analysis_fail(user_id, job_id, "internal", f"{type(e).__name__}: {e}")
        print(f"[worker] end    user={user_id} job={job_id} status={status} in {time.time() - started:.1f}s", flush=True)
        try:
            _notify(user_id, job_id, status)
        except Exception:
            traceback.print_exc()
    finally:
        _current = None
    return True


def _worker_loop():
    while True:
        try:
            run_once()
        except Exception:
            traceback.print_exc()
            time.sleep(IDLE_SECONDS)     # a persistent failure (say, a locked DB) must not spin


def ready_users():
    """Users whose master document is complete enough to generate from."""
    return [u for u in db.users_all() if pipeline.master_doc_ready(u["id"])]


def run_fetch():
    """Fetch, then create pending rows for what's new. Returns False if a
    fetch is already running."""
    if fetch_state["running"]:
        return False
    fetch_state.update(running=True, started_ts=time.time(), ok=None, error="")
    try:
        summary = fetch_ats.main()
        created = db.analysis_enqueue_missing(time.time() - NEW_ROW_WINDOW_DAYS * 86400, users=ready_users())
        fetch_state.update(ok=True, rows_created=created, **summary)
    except Exception as e:
        traceback.print_exc()
        fetch_state.update(ok=False, error=f"{type(e).__name__}: {e}")
    finally:
        fetch_state.update(running=False, finished_ts=time.time())
    return True


def fetch_now():
    """Ask the fetch thread to run right away. False if one is already running."""
    if fetch_state["running"]:
        return False
    _fetch_wanted.set()
    return True


def _fetch_loop():
    while True:
        run_fetch()
        _fetch_wanted.wait(FETCH_EVERY_SECONDS)
        _fetch_wanted.clear()


def start():
    """Call once, from the one server process. Whatever was in_process died
    with the previous process, so put it back before anything runs."""
    n = db.analysis_reset_interrupted()
    if n:
        print(f"[worker] {n} job(s) interrupted by the last shutdown, back to retry", flush=True)
    threading.Thread(target=_worker_loop, name="worker", daemon=True).start()
    threading.Thread(target=_fetch_loop, name="fetcher", daemon=True).start()
