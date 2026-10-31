"""Tests for db.py against a throwaway database. Run: python3 test_db.py"""
import os
import tempfile
import threading
import unittest

_tmp = tempfile.mkdtemp()
os.environ["JOBSEARCH_DB"] = os.path.join(_tmp, "t.db")
os.environ["ALLOWED_LOGIN_EMAIL"] = "admin@x.com"

import db  # noqa: E402


def mkjob(ext, company="Acme", title="Senior Software Engineer", loc="Bengaluru, India", source="greenhouse"):
    return db.jobs_add(source, ext, company, title, f"https://x/{ext}", "desc", loc)


class DbTest(unittest.TestCase):
    def setUp(self):
        for t in ("analysis", "jobs", "gap_cluster", "users"):
            db._run(f"DELETE FROM {t}" + (" WHERE role != 'admin'" if t == "users" else ""))
        self.admin = db.users_get("admin@x.com")["id"]
        self.bob = db.users_add("Bob@X.com")["id"]

    def test_admin_seeded_and_email_normalised(self):
        self.assertEqual(db.users_get("ADMIN@x.com")["role"], "admin")
        self.assertEqual(db.users_get("bob@x.com")["id"], self.bob)
        db.users_remove("admin@x.com")
        self.assertIsNotNone(db.users_get("admin@x.com"))      # admins can't be removed

    def test_jobs_add_dedups_on_company_and_external_id(self):
        a, new_a = mkjob("1")
        b, new_b = mkjob("1")
        c, new_c = mkjob("1", company="Beta")
        self.assertEqual((new_a, new_b, new_c), (True, False, True))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        first = db.jobs_get(a)["first_seen_ts"]
        self.assertGreaterEqual(db.jobs_get(a)["last_seen_ts"], first)

    def test_manual_external_id_is_content_identity(self):
        self.assertEqual(db.manual_external_id("A", "T", "x  y"), db.manual_external_id("a", "t", "X Y"))

    def test_enqueue_missing_is_idempotent_and_filters(self):
        mkjob("1")
        mkjob("2", title="Product Designer")                 # not engineering
        mkjob("3", loc="Austin, US")                          # wrong location
        mkjob("4", title="Engineering Manager")               # manager tier
        self.assertEqual(db.analysis_enqueue_missing(0), 2)   # job 1 for admin and bob
        self.assertEqual(db.analysis_enqueue_missing(0), 0)   # second run creates nothing
        self.assertEqual(len(db.analysis_pending()), 2)

    def test_claim_is_exclusive_even_across_threads(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        wins = []
        ts = [threading.Thread(target=lambda: wins.append(db.analysis_claim(self.admin, jid))) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(wins), 1)
        self.assertEqual(db.analysis_attempt(self.admin, jid), 1)

    def test_repull_never_resets_status(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        db.analysis_claim(self.admin, jid)
        db.analysis_ensure(self.admin, jid)
        self.assertEqual(db.analysis_get(self.admin, jid)["status"], "in_process")

    def test_retry_then_failed_after_three_attempts(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        statuses = []
        for _ in range(3):
            self.assertTrue(db.analysis_claim(self.admin, jid))
            statuses.append(db.analysis_fail(self.admin, jid, "timeout", "timeout"))
        self.assertEqual(statuses, ["retry", "retry", "failed"])
        self.assertFalse(db.analysis_claim(self.admin, jid))   # no fourth attempt
        a = db.analysis_get(self.admin, jid)
        self.assertEqual((a["status"], a["error_kind"], a["attempts"]), ("failed", "timeout", 3))
        self.assertEqual(db.analysis_pending(), [])

    def test_permanent_failure_skips_retries(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        db.analysis_claim(self.admin, jid)
        self.assertEqual(db.analysis_fail(self.admin, jid, "invalid", "no point bank", permanent=True), "failed")

    def _complete(self, uid, jid, pid=None):
        db.analysis_complete(
            uid, jid, 77, "why", "zai", "r.pdf", "c.pdf",
            requirements=[{"requirement": "Go", "importance": "required", "presence": "yes", "depth": "named",
                           "evidence_point_id": pid, "weight": 60, "earned": 40}],
            gaps=[{"gap": "Rust", "why": "none", "example": "ship one"}],
            projects=[], items=[])

    def test_complete_writes_everything_or_nothing(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        with self.assertRaises(RuntimeError):                  # not in_process yet
            self._complete(self.admin, jid)
        self.assertEqual(db.analysis_get(self.admin, jid)["requirements"], [])
        db.analysis_claim(self.admin, jid)
        pid = 7                                                # a point id in the user's master document
        self._complete(self.admin, jid, pid)
        a = db.analysis_get(self.admin, jid)
        self.assertEqual((a["status"], a["score"], len(a["requirements"]), len(a["gaps"])), ("done", 77, 1, 1))
        self.assertEqual(a["requirements"][0]["evidence_point_id"], pid)

    def test_failed_complete_rolls_back_children(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        db.analysis_claim(self.admin, jid)
        with self.assertRaises(Exception):                     # bad importance trips the CHECK mid-transaction
            db.analysis_complete(self.admin, jid, 50, "r", "zai", "r.pdf", "c.pdf",
                                 requirements=[{"requirement": "x", "importance": "bogus", "presence": "yes",
                                                "depth": "named", "weight": 1, "earned": 1}],
                                 gaps=[{"gap": "g"}], projects=[], items=[])
        a = db.analysis_get(self.admin, jid)
        self.assertEqual((a["status"], a["gaps"]), ("in_process", []))

    def test_regenerate_and_clear(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        db.analysis_claim(self.admin, jid)
        self.assertEqual(db.analysis_regenerate(self.admin, jid), (False, []))   # refused while in_process
        self._complete(self.admin, jid)
        ok, old = db.analysis_regenerate(self.admin, jid)
        a = db.analysis_get(self.admin, jid)
        self.assertEqual((ok, old, a["status"], a["attempts"], a["score"], a["gaps"]),
                         (True, ["r.pdf", "c.pdf"], "pending", 0, None, []))
        self.assertEqual(len(db.analysis_pending()), 1)
        ok, _ = db.analysis_clear(self.admin, jid)
        self.assertEqual((ok, db.analysis_get(self.admin, jid)["status"]), (True, "cleared"))
        self.assertEqual(db.analysis_pending(), [])            # cleared is not queued
        db.analysis_regenerate(self.bob, jid)                  # no row yet: creates a pending one
        self.assertEqual(db.analysis_get(self.bob, jid)["status"], "pending")

    def test_restart_resets_in_process(self):
        j1, _ = mkjob("1")
        j2, _ = mkjob("2")
        for j in (j1, j2):
            db.analysis_ensure(self.admin, j)
            db.analysis_claim(self.admin, j)
        for _ in range(2):                                     # j2 has now used all 3 attempts
            db.analysis_fail(self.admin, j2, "llm", "x")
            db.analysis_claim(self.admin, j2)
        self.assertEqual(db.analysis_reset_interrupted(), 1)
        self.assertEqual(db.analysis_get(self.admin, j1)["status"], "retry")
        a2 = db.analysis_get(self.admin, j2)
        self.assertEqual((a2["status"], a2["error_kind"]), ("failed", "interrupted"))

    def test_manual_jobs_are_polled_first(self):
        auto, _ = mkjob("1")
        man, _ = mkjob(db.manual_external_id("M", "T", "t"), company="M", title="Anything", loc="", source="manual")
        db.analysis_ensure(self.admin, auto)
        db.analysis_ensure(self.admin, man)
        self.assertEqual([r["job_id"] for r in db.analysis_pending()], [man, auto])

    def test_listing_isolates_users_and_hides_applied(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.admin, jid)
        total, _, rows = db.jobs_page(self.admin)
        self.assertEqual((total, rows[0]["status"]), (1, "pending"))
        _, _, rows = db.jobs_page(self.bob)
        self.assertIsNone(rows[0]["status"])                   # bob has no analysis row
        db.application_mark_applied(self.admin, jid, "linkedin")
        self.assertEqual(db.jobs_page(self.admin)[0], 0)
        self.assertEqual(db.jobs_page(self.bob)[0], 1)

    def test_listing_filters(self):
        mkjob("1", company="Acme", title="Senior Software Engineer")
        mkjob("2", company="Beta", title="Marketing Lead")
        self.assertEqual(db.jobs_page(self.admin)[0], 1)                       # eng_only default
        self.assertEqual(db.jobs_page(self.admin, eng_only=False)[0], 2)
        self.assertEqual(db.jobs_page(self.admin, company="beta", eng_only=False)[0], 1)
        self.assertEqual(db.jobs_page(self.admin, status="none")[0], 1)
        self.assertEqual({c["company"] for c in db.jobs_companies(self.admin, eng_only=False)}, {"Acme", "Beta"})

    def test_application_is_idempotent_with_events(self):
        jid, _ = mkjob("1")
        a1 = db.application_mark_applied(self.admin, jid, "referral", "Sam")
        a2 = db.application_mark_applied(self.admin, jid, "other")
        self.assertEqual(a1, a2)
        db.application_add_event(self.admin, a1, "interview", "onsite")
        app = db.application_all(self.admin)[0]
        self.assertEqual((app["status"], [e["status"] for e in app["events"]]), ("interview", ["applied", "interview"]))
        with self.assertRaises(ValueError):
            db.application_add_event(self.bob, a1, "offer")    # someone else's application

    def test_gap_clusters_roundtrip(self):
        j1, _ = mkjob("1")
        j2, _ = mkjob("2")
        db.gap_clusters_set(self.admin, [{"label": "Rust", "job_ids": [j1, j2]}, {"label": "K8s", "job_ids": [j1]}])
        got = db.gap_clusters_get(self.admin)
        self.assertEqual([(c["label"], c["count"]) for c in got["clusters"]], [("Rust", 2), ("K8s", 1)])
        db.gap_clusters_set(self.admin, [{"label": "Only", "job_ids": [j1]}])
        self.assertEqual(len(db.gap_clusters_get(self.admin)["clusters"]), 1)
        self.assertIsNone(db.gap_clusters_get(self.bob))

    def test_removing_user_cascades(self):
        jid, _ = mkjob("1")
        db.analysis_ensure(self.bob, jid)
        db.users_remove("bob@x.com")
        self.assertEqual(db._all("SELECT * FROM analysis WHERE user_id = ?", self.bob), [])

    def test_user_ids_are_never_reused(self):
        """A master document file is named by the user id, so a removed user's
        id must never be handed to the next person added."""
        newest = db.users_add("carol@x.com")["id"]
        db.users_remove("carol@x.com")
        self.assertGreater(db.users_add("dave@x.com")["id"], newest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
