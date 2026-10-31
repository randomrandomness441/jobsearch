"""Tests for worker.py: queue ordering, claim-based dedup, refill from the DB,
restart recovery, and fetch -> pending rows. process_one and the network are
stubbed. Run: python3 test_worker.py"""
import os
import tempfile
import time
import unittest

_tmp = tempfile.mkdtemp()
os.environ["JOBSEARCH_DATA"] = _tmp
os.environ["ALLOWED_LOGIN_EMAIL"] = "admin@x.com"

import db  # noqa: E402
import master  # noqa: E402
import notify_telegram  # noqa: E402
import pipeline  # noqa: E402
import worker  # noqa: E402

PROFILE = {"name": "T", "title": "SWE", "email": "t@x.com", "phone": "1", "linkedin": "l", "github": "g",
           "education": "e"}


def mkjob(ext, source="greenhouse", title="Senior Software Engineer", loc="Bengaluru, India"):
    return db.jobs_add(source, ext, "Acme", title, f"https://x/{ext}", "desc", loc)[0]


class WorkerTest(unittest.TestCase):
    def setUp(self):
        for t in ("analysis", "jobs", "users"):
            db._run(f"DELETE FROM {t}" + (" WHERE role != 'admin'" if t == "users" else ""))
        self.uid = db.users_get("admin@x.com")["id"]
        master.delete(self.uid)
        while not worker._queue.empty():
            worker._queue.get()
        self.processed = []
        self._real = (pipeline.process_one, worker._notify)
        pipeline.process_one = lambda u, j: (self.processed.append((u, j)), self._finish(u, j))[1]
        worker._notify = lambda *a: self.notified.append(a)
        self.notified = []

    def _finish(self, u, j):
        db.analysis_fail(u, j, "llm", "stub", permanent=True)      # any terminal state will do
        return "failed"

    def tearDown(self):
        pipeline.process_one, worker._notify = self._real

    def test_enqueue_claims_once(self):
        j = mkjob("1")
        db.analysis_ensure(self.uid, j)
        self.assertTrue(worker.enqueue(self.uid, j, manual=False))
        self.assertFalse(worker.enqueue(self.uid, j, manual=True))   # already in_process: not queued again
        self.assertEqual(worker._queue.qsize(), 1)

    def test_manual_goes_ahead_of_auto_but_not_ahead_of_running(self):
        autos = [mkjob(str(i)) for i in range(3)]
        for j in autos:
            db.analysis_ensure(self.uid, j)
            worker.enqueue(self.uid, j, manual=False)
        man = mkjob("m", source="manual")
        db.analysis_ensure(self.uid, man)
        worker.enqueue(self.uid, man, manual=True)
        while worker.run_once(wait=0.01):
            pass
        self.assertEqual([j for _, j in self.processed], [man] + autos)   # manual first, autos in arrival order

    def test_empty_queue_refills_from_pending_and_retry_rows(self):
        a, b, c = mkjob("1"), mkjob("2"), mkjob("3")
        for j in (a, b, c):
            db.analysis_ensure(self.uid, j)
        db._run("UPDATE analysis SET status = 'retry', attempts = 1 WHERE job_id = ?", b)
        db._run("UPDATE analysis SET status = 'done', resume_path = 'x', cover_path = 'y', finished_ts = 1 WHERE job_id = ?", c)
        self.assertTrue(worker.run_once(wait=0.01))
        self.assertTrue(worker.run_once(wait=0.01))
        self.assertFalse(worker.run_once(wait=0.01))                     # done row was never picked up
        self.assertEqual({j for _, j in self.processed}, {a, b})

    def test_refill_claims_at_most_one_batch_and_prefers_manual(self):
        for i in range(worker.BATCH + 5):
            db.analysis_ensure(self.uid, mkjob(f"a{i}"))
        man = mkjob("m", source="manual")
        db.analysis_ensure(self.uid, man)
        worker.run_once(wait=0.01)
        self.assertEqual(self.processed[0][1], man)
        in_process = db._one("SELECT COUNT(*) AS n FROM analysis WHERE status = 'in_process'")["n"]
        self.assertEqual(in_process, worker.BATCH - 1)                  # claimed batch minus the one already run
        self.assertEqual(db._one("SELECT COUNT(*) AS n FROM analysis WHERE status = 'pending'")["n"], 5 + 1)

    def test_polling_the_same_rows_twice_never_double_queues(self):
        for i in range(3):
            db.analysis_ensure(self.uid, mkjob(str(i)))
        for row in db.analysis_pending():
            worker.enqueue(row["user_id"], row["job_id"], False)
        for row in db.analysis_pending():                                  # claimed rows are no longer pending
            worker.enqueue(row["user_id"], row["job_id"], False)
        self.assertEqual(worker._queue.qsize(), 3)

    def test_a_crash_inside_process_one_releases_the_row(self):
        j = mkjob("1")
        db.analysis_ensure(self.uid, j)
        pipeline.process_one = lambda u, j: 1 / 0
        self.assertTrue(worker.run_once(wait=0.01))
        a = db.analysis_get(self.uid, j)
        self.assertEqual((a["status"], a["error_kind"]), ("retry", "internal"))   # not stuck in_process
        self.assertIsNone(worker._current)

    def test_start_recovers_in_process_rows(self):
        j = mkjob("1")
        db.analysis_ensure(self.uid, j)
        db.analysis_claim(self.uid, j)
        self.assertEqual(db.analysis_reset_interrupted(), 1)
        self.assertEqual(db.analysis_get(self.uid, j)["status"], "retry")
        self.assertTrue(worker.run_once(wait=0.01))

    def test_ready_users_and_fetch_creates_rows_only_for_them(self):
        bob = db.users_add("bob@x.com")["id"]
        for k, v in PROFILE.items():
            master.profile_set(self.uid, k, v)
        master.point_bank_add(self.uid, "Acme", "SWE", "2020", None, "did a thing")
        self.assertEqual([u["id"] for u in worker.ready_users()], [self.uid])   # bob has no master doc
        import fetch_ats
        real = fetch_ats.main
        fetch_ats.main = lambda: (mkjob("f1"), mkjob("f2", title="Product Designer"),
                                  {"new": 2, "seen": 0, "errors": 0})[2]
        try:
            self.assertTrue(worker.run_fetch())
        finally:
            fetch_ats.main = real
        self.assertEqual(worker.fetch_state["rows_created"], 1)             # the designer role isn't eligible
        self.assertEqual(db._all("SELECT user_id FROM analysis"), [{"user_id": self.uid}])
        self.assertTrue(worker.fetch_state["ok"])
        self.assertIsNone(db._one("SELECT 1 AS x FROM analysis WHERE user_id = ?", bob))

    def test_a_user_with_an_unreadable_master_document_is_simply_not_ready(self):
        for k, v in PROFILE.items():
            master.profile_set(self.uid, k, v)
        master.point_bank_add(self.uid, "Acme", "SWE", "2020", None, "did a thing")
        self.assertEqual([u["id"] for u in worker.ready_users()], [self.uid])
        with open(master.path(self.uid), "w") as f:
            f.write("not json")
        self.assertEqual(worker.ready_users(), [])                          # no crash, no rows created for them

    def test_fetch_failure_is_reported_not_raised(self):
        import fetch_ats
        real = fetch_ats.main
        fetch_ats.main = lambda: 1 / 0
        try:
            worker.run_fetch()
        finally:
            fetch_ats.main = real
        self.assertFalse(worker.fetch_state["ok"])
        self.assertIn("ZeroDivisionError", worker.fetch_state["error"])
        self.assertFalse(worker.fetch_state["running"])                     # the flag always clears

    def test_notify_only_for_auto_jobs_with_a_telegram_config(self):
        real_notify = self._real[1]
        sent = []
        real_done, real_fail = notify_telegram.send_generation_complete, notify_telegram.send_generation_failed
        notify_telegram.send_generation_complete = lambda job, r, c, **kw: sent.append(("done", job["id"], r, c))
        notify_telegram.send_generation_failed = lambda job, why, **kw: sent.append(("failed", job["id"], why))
        os.environ["JOBSEARCH_BASE_URL"] = "https://app"
        try:
            auto, man = mkjob("1"), mkjob("2", source="manual")
            for j in (auto, man):
                db.analysis_ensure(self.uid, j)
            real_notify(self.uid, auto, "done")                              # no telegram config yet
            self.assertEqual(sent, [])
            db.telegram_config_set(self.uid, "tok", "chat")
            real_notify(self.uid, man, "done")                               # manual: owner is on the page
            real_notify(self.uid, auto, "retry")                             # not final
            self.assertEqual(sent, [])
            real_notify(self.uid, auto, "done")
            self.assertEqual(sent[0][:2], ("done", auto))
            self.assertIn(f"user={self.uid}&job_id={auto}&kind=resume&exp=", sent[0][2])
            db.analysis_claim(self.uid, auto)
            db.analysis_fail(self.uid, auto, "timeout", "timeout", permanent=True)
            real_notify(self.uid, auto, "failed")
            self.assertEqual(sent[1], ("failed", auto, "timeout"))
        finally:
            notify_telegram.send_generation_complete, notify_telegram.send_generation_failed = real_done, real_fail


if __name__ == "__main__":
    unittest.main(verbosity=2)
