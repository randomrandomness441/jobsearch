"""Tests for the fetch-time filters in fetch_ats.main(): what is stored is
exactly what should get a resume. The network is replaced by canned boards;
the filters, the counting and the database writes are the real ones.
Run: python3 test_fetch.py"""
import os
import tempfile
import time
import unittest

_tmp = tempfile.mkdtemp()
os.environ["JOBSEARCH_DATA"] = _tmp
os.environ["ALLOWED_LOGIN_EMAIL"] = "admin@x.com"

import ats  # noqa: E402
import db  # noqa: E402
import fetch_ats  # noqa: E402

DAY = 86400
_n = [0]


def posting(title="Software Engineer", location="Bengaluru, India", desc="Build services.", age_days=1, ext=None):
    _n[0] += 1
    return {"external_id": ext or str(_n[0]), "title": title, "url": "https://x", "description_raw": desc,
            "location": location, "posted_ts": None if age_days is None else time.time() - age_days * DAY}


class FetchTest(unittest.TestCase):
    def setUp(self):
        db._run("DELETE FROM analysis")
        db._run("DELETE FROM jobs")
        self.boards = {}
        self._real = (fetch_ats._fetch_one, fetch_ats.COMPANIES)
        fetch_ats.COMPANIES = [("Acme", "greenhouse", "acme")]

        def fake(row):
            board = self.boards[row[0]]
            if isinstance(board, Exception):
                return row[0], row[1], row[2], None, board
            return row[0], row[1], row[2], board, None
        fetch_ats._fetch_one = fake

    def tearDown(self):
        fetch_ats._fetch_one, fetch_ats.COMPANIES = self._real

    def stored_titles(self):
        return sorted(r["title"] for r in db._all("SELECT title FROM jobs"))

    def run_fetch(self, *postings):
        self.boards["Acme"] = list(postings)
        return fetch_ats.main()

    # ── location: Bengaluru, Bangalore, Remote ───────────────────────────
    def test_bengaluru_bangalore_and_remote_locations_are_kept(self):
        for i, loc in enumerate(["Bengaluru, Karnataka, India", "Bangalore", "BANGALORE, IN", "Remote",
                                 "Remote - India", "India (Remote)", "Hybrid - Bengaluru"]):
            self.run_fetch(posting(location=loc, ext=f"keep{i}"))
        self.assertEqual(db._one("SELECT COUNT(*) n FROM jobs")["n"], 7)

    def test_other_locations_are_dropped_and_counted(self):
        r = self.run_fetch(*[posting(location=loc) for loc in
                             ["Hyderabad, India", "Pune", "Austin, TX", "Indiana, USA", "London, UK",
                              "Remotely hosted office"]])
        self.assertEqual((r["new"], r["skipped_location"]), (0, 6))
        self.assertEqual(db._one("SELECT COUNT(*) n FROM jobs")["n"], 0)

    def test_a_posting_with_no_location_is_judged_on_the_start_of_its_description(self):
        r = self.run_fetch(posting(location="", desc="Join our Bengaluru office. " + "x" * 600, title="Software Engineer"),
                           posting(location="", desc="y" * 600 + " Bengaluru", title="Senior Software Engineer"),
                           posting(location="", desc="Austin based team", title="Software Engineer 2"))
        self.assertEqual(self.stored_titles(), ["Software Engineer"])       # a mention past 500 chars doesn't count
        self.assertEqual(r["skipped_location"], 2)

    # ── title ────────────────────────────────────────────────────────────
    def test_engineering_titles_are_kept(self):
        titles = ["Software Engineer", "Senior Software Engineer", "Software Engineer 2", "Software Engineer 3",
                  "Backend Engineer", "Data Engineer", "SDE 1", "SDE 2", "SDE 3", "SDE-2", "MTS", "Member of Technical Staff"]
        self.run_fetch(*[posting(title=t) for t in titles])
        self.assertEqual(self.stored_titles(), sorted(titles))

    def test_engineer_titles_outside_the_list_are_dropped(self):
        r = self.run_fetch(*[posting(title=t) for t in
                             ["Frontend Engineer", "Machine Learning Engineer", "Site Reliability Engineer", "SDE", "SDE 4"]])
        self.assertEqual((r["new"], r["skipped_title"]), (0, 5))

    def test_years_of_experience_in_the_description_do_not_filter_anything(self):
        r = self.run_fetch(posting(title="Software Engineer", desc="2-4 years of experience", ext="a"),
                           posting(title="Software Engineer", desc="6+ years required", ext="b"),
                           posting(title="Software Engineer", desc="10+ years", ext="c"))
        self.assertEqual(r["new"], 3)

    def test_non_engineering_manager_and_non_building_titles_are_dropped_and_counted(self):
        r = self.run_fetch(*[posting(title=t) for t in
                             ["Product Designer", "Engineering Manager", "Director of Engineering",
                              "Software Engineer, Intern", "Solutions Engineer", "Sales Engineer", "Account Executive"]])
        self.assertEqual((r["new"], r["skipped_title"], r["skipped_location"]), (0, 7, 0))

    def test_manager_titles_that_look_like_engineering_roles_are_still_dropped(self):
        """These contain a target phrase; only the manager-tier rule keeps them out."""
        titles = ["Software Engineer Manager", "Manager, Software Engineer", "VP, Software Engineer",
                  "Software Engineer - Director", "SDE 2 Manager", "Backend Engineer Manager", "Data Engineer Manager"]
        for t in titles:
            self.assertTrue(ats.is_target_title(t), t)                   # guards the premise of this test
        r = self.run_fetch(*[posting(title=t) for t in titles])
        self.assertEqual((r["new"], r["skipped_title"]), (0, 7))

    def test_a_wrong_location_is_counted_as_location_not_title(self):
        r = self.run_fetch(posting(title="Product Designer", location="Austin"))
        self.assertEqual((r["skipped_location"], r["skipped_title"]), (1, 0))

    # ── age ──────────────────────────────────────────────────────────────
    def test_postings_older_than_the_limit_are_dropped_and_an_unreadable_date_is_kept(self):
        limit = fetch_ats.MAX_POSTING_AGE_DAYS
        r = self.run_fetch(posting(title="Fresh Software Engineer", age_days=limit - 1),
                           posting(title="Stale Software Engineer", age_days=limit + 1),
                           posting(title="Undated Software Engineer", age_days=None))
        self.assertEqual(self.stored_titles(), ["Fresh Software Engineer", "Undated Software Engineer"])
        self.assertEqual(r["skipped_age"], 1)

    def test_the_age_limit_is_a_month(self):
        self.assertEqual(fetch_ats.MAX_POSTING_AGE_DAYS, 30)

    # ── storing ──────────────────────────────────────────────────────────
    def test_a_posting_without_a_description_is_ignored(self):
        r = self.run_fetch(posting(desc=""))
        self.assertEqual((r["new"], db._one("SELECT COUNT(*) n FROM jobs")["n"]), (0, 0))

    def test_stored_rows_carry_source_listing_id_company_and_fetch_dates(self):
        self.run_fetch(posting(ext="abc-1"))
        row = db._one("SELECT * FROM jobs")
        self.assertEqual((row["source"], row["external_id"], row["company"]), ("greenhouse", "abc-1", "Acme"))
        self.assertAlmostEqual(row["first_seen_ts"], time.time(), delta=30)
        self.assertEqual(row["first_seen_ts"], row["last_seen_ts"])

    def test_fetching_the_same_listing_again_stores_nothing_new_and_moves_only_last_seen(self):
        r1 = self.run_fetch(posting(ext="same"))
        first = db._one("SELECT * FROM jobs")
        time.sleep(0.02)
        r2 = self.run_fetch(posting(ext="same"))
        again = db._one("SELECT * FROM jobs")
        self.assertEqual((r1["new"], r2["new"], r2["seen"]), (1, 0, 1))
        self.assertEqual(db._one("SELECT COUNT(*) n FROM jobs")["n"], 1)
        self.assertEqual(again["first_seen_ts"], first["first_seen_ts"])
        self.assertGreater(again["last_seen_ts"], first["last_seen_ts"])

    def test_one_failing_board_is_counted_and_does_not_stop_the_others(self):
        fetch_ats.COMPANIES = [("Broken", "lever", "b"), ("Acme", "greenhouse", "acme")]
        self.boards["Broken"] = RuntimeError("boom")
        self.boards["Acme"] = [posting()]
        r = fetch_ats.main()
        self.assertEqual((r["errors"], r["new"]), (1, 1))

    def test_nothing_that_fails_a_filter_is_ever_stored(self):
        self.run_fetch(posting(title="Software Engineer"), posting(title="Product Designer"),
                       posting(location="Austin"), posting(age_days=90), posting(title="Engineering Manager"))
        self.assertEqual(self.stored_titles(), ["Software Engineer"])

    # ── one source of truth ──────────────────────────────────────────────
    def test_fetch_and_the_classifier_share_the_same_location_rule(self):
        self.assertIs(fetch_ats.is_target_location, ats.is_target_location)

    def test_there_is_no_second_filter_after_fetch(self):
        """Whatever fetch stored gets a row; the database does not re-validate."""
        uid = db.users_get("admin@x.com")["id"]
        jid, _ = db.jobs_add("greenhouse", "x1", "Acme", "Product Designer", "u", "d", "Austin")   # fails every filter
        self.assertEqual(db.analysis_enqueue_missing(users=[db.users_get("admin@x.com")]), 1)
        self.assertIsNotNone(db.analysis_get(uid, jid))


if __name__ == "__main__":
    unittest.main(verbosity=2)
