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
    """Same filter as the AI-judge button, run once per whitelisted user:
    real engineering title, not manager-tier, target location, never-
    judged-for-THIS-user or judged-before-THIS-user's-own-master-doc-
    last-changed, fetched within the last 14 days. One shared jobs pool,
    but each user's own score on it -- a cron tick scores every
    whitelisted user's view of the same postings, not just the admin's."""
    date_cutoff = S._days_cutoff(None, default=14)
    for u in jobsdb.users_all():
        email = u["email"]
        profile, skill_names, _bullets_text, _skills_line, candidate_years = S._jobs_resume_body(email)
        point_bank_items = jobsdb.point_bank_all(email)
        candidates = [j for j in jobsdb.jobs_list(email)
                      if jobs_ats.is_likely_engineering_title(j["title"])
                      and jobs_ats.title_seniority_tier(j["title"]) != "manager"
                      and jobsdb.jobs_llm_score_stale(email, j)
                      and jobs_ats.is_target_location(j["location"], j["description_raw"])
                      and (date_cutoff is None or j["fetched_ts"] >= date_cutoff)]
        print(f"[poll] {email}: {len(candidates)} eligible for scoring")
        failed = 0
        for j in candidates:
            ok, reason = S._score_only(email, j, profile, point_bank_items, candidate_years,
                                        tuning_key="judge_batch")
            if not ok:
                failed += 1
                print(f"[poll] score failed for {email}, job {j['id']} ({j['title']} @ {j['company']}): {reason}",
                      file=sys.stderr)
        print(f"[poll] {email}: scored {len(candidates) - failed}/{len(candidates)}")


def notify_new_postings(run_started_ts):
    """Notify every whitelisted user, through THEIR OWN Telegram bot, for
    EVERY scored posting fetched in THIS cron tick -- no score threshold
    (was NOTIFY_THRESHOLD = 80; removed on request: every posting should
    produce a notification now, not just ones scoring 80+). A user with no
    telegram_config saved yet is skipped (logged, not an error) -- they
    just use the website instead until they fill in the Profile tab's
    Telegram box.

    Still gated on "genuinely fetched in this run" (fetched_ts >=
    run_started_ts, captured before fetch_new_postings() ran), not just
    "recently" -- jobs_add() only ever sets fetched_ts once, at insert, and
    never touches it for an already-tracked duplicate (confirmed in db.py),
    so this is an exact signal, not a time-window approximation. Without
    this, a rescore of anything already in the database (a manual AI-judge
    run, a master-doc edit, an explicit "Clear scores") would notify again
    too -- real incident: clearing scores on a 77-job backlog and
    re-judging it fired a 77-message Telegram burst for postings that were
    never actually new. That risk is UNCHANGED by removing the score
    threshold, so this gate stays.

    Still gated on "not already notified" -- same dedup as before, a
    posting only ever gets one notification per genuine fetch, per user.
    """
    for u in jobsdb.users_all():
        email = u["email"]
        cfg = jobsdb.telegram_config_get(email)
        if cfg is None:
            print(f"[poll] {email}: no Telegram config saved -- skipping notifications for this user")
            continue
        sent, skipped_old = 0, 0
        for j in jobsdb.jobs_list(email):
            if j["llm_score"] is None:
                continue
            if j["notified_ts"] is not None:
                continue
            if j["fetched_ts"] < run_started_ts:
                # Scored in this tick but fetched earlier (a rescore, not a fresh
                # fetch) -- don't alert. Not marked notified either, so it's still
                # eligible once a genuine future re-fetch of this exact posting
                # updates it.
                skipped_old += 1
                continue
            sig = S._sign_job_user(email, j["id"])
            generate_url = f"{BASE_URL}/api/jobs/generate-link?job_id={j['id']}&user={email}&sig={sig}"
            view_url = f"{BASE_URL}/api/jobs/view-details?job_id={j['id']}&user={email}&sig={sig}"
            ok, err = notify_telegram.send_job_alert(j, generate_url, view_url,
                                                       bot_token=cfg["bot_token"], chat_id=cfg["chat_id"])
            if ok:
                jobsdb.jobs_mark_notified(email, j["id"])
                sent += 1
                print(f"[poll] {email}: notified: {j['title']} @ {j['company']} (score {j['llm_score']:.0f})")
            else:
                print(f"[poll] {email}: notify FAILED for job {j['id']}: {err}", file=sys.stderr)
        print(f"[poll] {email}: {sent} notification(s) sent, {skipped_old} skipped "
              f"(scored but not fetched in this run)")


def main():
    lock = _acquire_lock()
    if lock is None:
        print("[poll] another run is already in progress -- skipping this tick")
        return
    try:
        run_started_ts = time.time()   # captured BEFORE fetch -- see notify_new_postings's docstring
        fetch_new_postings()
        score_eligible()
        notify_new_postings(run_started_ts)
    finally:
        lock.close()


if __name__ == "__main__":
    main()
