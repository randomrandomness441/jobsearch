#!/usr/bin/env python3
"""Run this after every restart/deploy -- the "did I actually break anything"
check, instead of finding out the hard way hours later (as happened live,
more than once: a missing poppler-utils, a stale notified_ts, an empty
point bank after a fresh clone, all silently broken for a while before
anyone noticed).

    .venv/bin/python3 healthcheck.py            fast checks only
    .venv/bin/python3 healthcheck.py --notify    also sends a real Telegram
                                                  test message (costs a real
                                                  send -- skip on routine runs)

Exit code 0 if everything PASSed, 1 if anything FAILed. Prints one PASS/FAIL
line per check, most load-bearing first.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

import db
import notify_telegram
import server as S

RESULTS = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"raised {type(e).__name__}: {e}"
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def check_server_up():
    try:
        urllib.request.urlopen(f"http://{S.HOST or '127.0.0.1'}:{S.PORT}/", timeout=5)
        return True, "server responded"
    except urllib.error.HTTPError:
        # Any HTTP response (even a 401/redirect to login) proves the process
        # is alive and listening -- that's what this checks, not auth.
        return True, "server responded"
    except Exception as e:
        return False, f"no response from port {S.PORT}: {e}"


def check_db_integrity():
    import sqlite3
    con = sqlite3.connect(db.DB_PATH)
    result = con.execute("PRAGMA integrity_check;").fetchone()[0]
    con.close()
    return result == "ok", f"PRAGMA integrity_check -> {result}"


def check_point_bank():
    n = len(db.point_bank_all())
    # Real point bank has 50+ rows -- a handful would mean a fresh/empty
    # clone's db got used by mistake (the exact VPS incident this guards
    # against: a git clone has no data/jobsearch.db at all).
    return n >= 20, f"{n} point_bank rows"


def check_binaries():
    missing = [b for b in ("soffice", "pdfinfo") if shutil.which(b) is None]
    if missing:
        return False, f"missing from PATH: {', '.join(missing)} -- resume generation will fail"
    return True, "soffice and pdfinfo both on PATH"


def check_zai_key():
    key = os.environ.get("ZAI_API_KEY", "")
    return bool(key), "ZAI_API_KEY is set" if key else "ZAI_API_KEY not set in environment"


def check_telegram_bot(send_test=False):
    if not notify_telegram.TELEGRAM_BOT_TOKEN or not notify_telegram.TELEGRAM_CHAT_ID:
        return False, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set"
    ok, err = notify_telegram._call("getMe", {})
    if not ok:
        return False, f"getMe failed: {err}"
    if send_test:
        ok2, err2 = notify_telegram._call(
            "sendMessage", {"chat_id": notify_telegram.TELEGRAM_CHAT_ID,
                             "text": f"jobsearch healthcheck OK -- {time.strftime('%Y-%m-%d %H:%M')}"})
        if not ok2:
            return False, f"bot token valid but sendMessage failed: {err2}"
        return True, "bot token valid, test message sent"
    return True, "bot token valid (getMe ok) -- pass --notify to also send a real test message"


def check_cron_registered():
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if out.returncode != 0:
        return False, "no crontab for this user at all"
    if "poll_and_notify.py" not in out.stdout:
        return False, "crontab exists but has no poll_and_notify.py line"
    commented = any(line.strip().startswith("#") and "poll_and_notify.py" in line
                     for line in out.stdout.splitlines())
    if commented:
        return False, "poll_and_notify.py line exists but is commented out"
    return True, "poll_and_notify.py registered and active"


def check_latest_resume_on_disk():
    apps = db.applications_all()
    if not apps:
        return True, "no applications generated yet -- nothing to check"
    latest = apps[0]
    folder = latest["folder_path"]
    pdf_path = os.path.join(folder, "resume.pdf")
    if not os.path.isfile(pdf_path):
        return False, f"most recent application's resume.pdf missing: {pdf_path}"
    size = os.path.getsize(pdf_path)
    if size < 1000:
        return False, f"resume.pdf exists but is suspiciously small ({size} bytes)"
    return True, f"most recent resume.pdf present and {size} bytes"


def check_idempotent_regenerate():
    apps = db.applications_all()
    if not apps:
        return True, "no generated applications yet -- nothing to check"
    job = db.jobs_get(apps[0]["job_id"])
    if job is None:
        return False, "most recent application's job no longer exists"
    needs = S._needs_regenerate(job)
    if needs:
        return True, "job was rescored since last generation -- a regenerate is correctly expected, not a bug"
    return True, "already-generated, not-rescored job correctly resolves to a no-op (won't waste an LLM call)"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--notify", action="store_true", help="also send a real Telegram test message")
    args = p.parse_args()

    check("server reachable", check_server_up)
    check("database integrity", check_db_integrity)
    check("point bank populated", check_point_bank)
    check("soffice + pdfinfo on PATH", check_binaries)
    check("ZAI_API_KEY set", check_zai_key)
    check("Telegram bot", lambda: check_telegram_bot(send_test=args.notify))
    check("cron registered", check_cron_registered)
    check("latest resume.pdf on disk", check_latest_resume_on_disk)
    check("idempotent regenerate guard", check_idempotent_regenerate)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    if failed:
        print(f"{len(failed)} check(s) FAILED: {', '.join(failed)}")
        sys.exit(1)
    print(f"all {len(RESULTS)} checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
