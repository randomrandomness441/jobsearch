#!/usr/bin/env python3
"""Cron entrypoint: fetch new postings, score whatever's eligible, and
send a Telegram notification for any job that newly crosses the fit-score
threshold. Deploy with a real OS crontab entry (NOT Claude Code's own
scheduler -- confirmed session-only, 7-day auto-expiry, wrong tool for an
always-on pipeline):

    */10 * * * * cd /path/to/jobsearch && .venv/bin/python3 poll_and_notify.py >> data/poll.log 2>&1

Reuses the exact same eligibility filter and scoring call the "AI-judge"
button already uses (see server.py's /api/jobs/llm-backfill route) --
no separate scoring logic to drift out of sync with the real app.

Lock file prevents two overlapping runs from double-scoring the same job
if one run ever takes longer than the 10-minute cadence -- skips cleanly
rather than racing. Nothing is lost by skipping: the next tick's fetch
still asks "what's new since last time," not "what showed up in exactly
this window," so a skipped tick costs a short delay, never a missed
posting. See the deploy design notes in the repo (and this session's
plan) for the fuller reasoning.
"""
import fcntl
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import server as S  # noqa: E402
import db as jobsdb  # noqa: E402
import ats as jobs_ats  # noqa: E402
import notify_telegram  # noqa: E402

NOTIFY_THRESHOLD = 80
LOCK_PATH = os.path.join(ROOT, "data", "poll.lock")
# Overridable so a local/manual run can point at a different base URL than
# the real deployed one; the real deploy sets this to the actual public
# site address once it exists.
BASE_URL = os.environ.get("JOBSEARCH_BASE_URL", f"http://localhost:{S.PORT}")


def _acquire_lock():
    """An open, exclusively-locked file handle, or None if another run is
    still in progress. Never explicitly released -- closing the process
    (normal exit OR a crash) releases the OS-level lock automatically,
    which is exactly the "self-heals on crash" property this needs."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def fetch_new_postings():
    out = subprocess.run([S.VENV_PY, "fetch_ats.py"], capture_output=True,
                          text=True, timeout=600, cwd=ROOT)
    if out.returncode != 0:
        print(f"[poll] fetch failed (exit {out.returncode}): {out.stderr[-500:]}", file=sys.stderr)
        return False
    # Was logging ONLY the last line (the one-line summary) and silently discarding
    # fetch_ats.py's full per-company output -- found live: with that, poll.log could
    # never show WHICH of the ~96 companies actually succeeded, errored, or returned
    # zero postings, even on a run where something was genuinely wrong with specific
    # boards. Log the whole thing (per-company lines + the real summary), so a bad
    # stretch is visible in poll.log itself, not just inferable from the one number.
    print(out.stdout.strip() or "[poll] fetch ok but produced no output at all")
    return True
    return out.returncode == 0


def score_eligible():
    """Same filter as the AI-judge button: real engineering title, not
    manager-tier, target location, never-judged or judged-before-the-
    master-doc-last-changed, fetched within the last 14 days."""
    profile, skill_names, _bullets_text, _skills_line, candidate_years = S._jobs_resume_body()
    point_bank_items = jobsdb.point_bank_all()
    date_cutoff = S._days_cutoff(None, default=14)
    candidates = [j for j in jobsdb.jobs_list()
                  if jobs_ats.is_likely_engineering_title(j["title"])
                  and jobs_ats.title_seniority_tier(j["title"]) != "manager"
                  and jobsdb.jobs_llm_score_stale(j)
                  and jobs_ats.is_target_location(j["location"], j["description_raw"])
                  and (date_cutoff is None or j["fetched_ts"] >= date_cutoff)]
    print(f"[poll] {len(candidates)} eligible for scoring")
    failed = 0
    for j in candidates:
        ok, reason = S._score_only(j, profile, point_bank_items, candidate_years, tuning_key="judge_batch")
        if not ok:
            failed += 1
            print(f"[poll] score failed for job {j['id']} ({j['title']} @ {j['company']}): {reason}",
                  file=sys.stderr)
    print(f"[poll] scored {len(candidates) - failed}/{len(candidates)}")


def notify_crossed_threshold(run_started_ts):
    """Only a posting genuinely fetched in THIS cron tick (fetched_ts >=
    run_started_ts, captured before fetch_new_postings() ran) is eligible
    for a Telegram alert at all -- not "recently", THIS run, full stop.
    jobs_add() only sets fetched_ts once, at insert, and never touches it
    for an already-tracked duplicate (confirmed in db.py), so this is an
    exact signal, not a time-window approximation.

    Real incident this replaces a looser "last 14 days" version of:
    clearing scores on a 77-job backlog and re-judging it crossed the
    threshold on all of them at once, firing a 77-message Telegram burst
    for postings that were never actually new. A rescore of ANYTHING
    already in the database -- a minute old or a year old -- must never
    be able to trigger a notification on its own; only a genuine new
    fetch can.
    """
    sent, cleared, skipped_old = 0, 0, 0
    for j in jobsdb.jobs_list():
        if j["llm_score"] is None:
            continue
        already_notified = j["notified_ts"] is not None
        is_from_this_run = j["fetched_ts"] >= run_started_ts
        if j["llm_score"] >= NOTIFY_THRESHOLD:
            if already_notified:
                continue
            if not is_from_this_run:
                # Crossed the threshold via a rescore, not a fresh fetch -- don't
                # alert. Not marked notified either, so it stays visible/available
                # in the UI; a LATER genuine re-fetch of this exact posting would
                # just update this same row (same dedup key), never reset
                # notified_ts anyway, so there's nothing to "come back to" here.
                skipped_old += 1
                continue
            sig = S._sign_job_id(j["id"])
            generate_url = f"{BASE_URL}/api/jobs/generate-link?job_id={j['id']}&sig={sig}"
            view_url = f"{BASE_URL}/api/jobs/view-details?job_id={j['id']}&sig={sig}"
            ok, err = notify_telegram.send_job_alert(j, generate_url, view_url)
            if ok:
                jobsdb.jobs_mark_notified(j["id"])
                sent += 1
                print(f"[poll] notified: {j['title']} @ {j['company']} (score {j['llm_score']:.0f})")
            else:
                print(f"[poll] notify FAILED for job {j['id']}: {err}", file=sys.stderr)
        elif already_notified:
            # Score dropped back below threshold (master doc or JD
            # changed) -- clear the flag so a genuine re-crossing later
            # notifies again instead of being silently swallowed forever
            # by the first notification.
            jobsdb.jobs_clear_notified(j["id"])
            cleared += 1
    print(f"[poll] {sent} notification(s) sent, {cleared} notified-flag(s) cleared "
          f"(score dropped back below {NOTIFY_THRESHOLD}), {skipped_old} skipped "
          f"(qualified but not fetched in this run)")


def main():
    lock = _acquire_lock()
    if lock is None:
        print("[poll] another run is already in progress -- skipping this tick")
        return
    try:
        run_started_ts = time.time()   # captured BEFORE fetch -- see notify_crossed_threshold's docstring
        fetch_new_postings()
        score_eligible()
        notify_crossed_threshold(run_started_ts)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
