"""Tests for the behaviour that was specified, at the HTTP layer: real handler,
throwaway database and data directory, no LLM. A test here fails if the feature
is missing -- nothing is stubbed that the feature itself is responsible for.
Run: python3 test_server.py"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

_tmp = tempfile.mkdtemp()
os.environ.update(JOBSEARCH_DATA=_tmp, ALLOWED_LOGIN_EMAIL="admin@x.com", SESSION_SECRET="sess-secret",
                  GENERATE_LINK_SECRET="link-secret")

import auth  # noqa: E402
import db  # noqa: E402
import links  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402
import server  # noqa: E402
import worker  # noqa: E402


def cookie(email):
    return {"Cookie": "session=" + auth.make_session_cookie(email)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        for t in ("analysis", "application", "jobs", "point_bank", "profile", "users"):
            db._run(f"DELETE FROM {t}" + (" WHERE role != 'admin'" if t == "users" else ""))
        shutil.rmtree(pipeline.FILES_DIR, ignore_errors=True)
        while not worker._queue.empty():
            worker._queue.get()
        self.uid = db.users_get("admin@x.com")["id"]
        self.admin = cookie("admin@x.com")

    def call(self, path, body=None, headers=None):
        headers = self.admin if headers is None else headers
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with OPENER.open(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def job(self, ext="1", title="Senior Software Engineer", company="Acme", source="greenhouse"):
        return db.jobs_add(source, ext, company, title, "https://x", "Python role", "Bengaluru, India")[0]

    def make_done(self, job_id, company="acme"):
        """A finished analysis with real files on disk, as the pipeline leaves it."""
        db.analysis_ensure(self.uid, job_id)
        self.assertTrue(db.analysis_claim(self.uid, job_id))
        db.analysis_start(self.uid, job_id)
        folder = os.path.join(pipeline.FILES_DIR, str(self.uid), company, str(job_id))
        os.makedirs(folder)
        for name in ("resume.pdf", "resume.docx", "cover_letter.pdf", "cover_letter.docx", "job_description.md"):
            with open(os.path.join(folder, name), "wb") as f:
                f.write(b"content-of-" + name.encode())
        rel = os.path.relpath(folder, db.DATA_DIR)
        db.analysis_complete(self.uid, job_id, 80, "good", "zai", f"{rel}/resume.pdf", f"{rel}/cover_letter.pdf",
                             requirements=[], gaps=[], projects=[], items=[])
        return folder

    # ── who may do what ──────────────────────────────────────────────────
    def test_not_logged_in_is_redirected_to_login_and_api_posts_are_401(self):
        code, _ = self.call("/api/jobs/list", headers={})
        self.assertEqual(code, 302)
        code, body = self.call("/api/jobs/manual", {"company": "A", "title": "B", "text": "C"}, headers={"X": "1"})
        self.assertEqual(code, 401)

    def test_forged_or_tampered_session_cookie_is_not_logged_in(self):
        good = auth.make_session_cookie("admin@x.com")
        email, exp, sig = good.split("|")
        for bad in (f"other@x.com|{exp}|{sig}", f"{email}|{exp}|{'0' * 64}", f"{email}|1|{sig}"):
            code, _ = self.call("/api/session", headers={"Cookie": "session=" + bad})
            self.assertEqual(code, 302, bad)

    def test_guest_can_browse_postings_but_cannot_act_or_read_private_data(self):
        self.job()
        guest = cookie("guest@x.com")
        code, body = self.call("/api/jobs/list", headers=guest)
        self.assertEqual((code, body["total_matched"]), (200, 1))
        self.assertEqual(body["jobs"][0]["status"], "none")
        for path, payload in (("/api/jobs/manual", {"company": "A", "title": "B", "text": "C"}),
                              ("/api/jobs/regenerate", {"job_id": 1}), ("/api/jobs/refetch", {})):
            self.assertEqual(self.call(path, payload, headers=guest)[0], 403, path)
        self.assertEqual(self.call("/api/profile/fields", headers=guest)[0], 403)
        self.assertEqual(self.call("/api/applications/list", headers=guest)[0], 403)

    def test_only_admin_manages_the_whitelist(self):
        db.users_add("member@x.com")
        member = cookie("member@x.com")
        self.assertEqual(self.call("/api/admin/users", headers=member)[0], 403)
        self.assertEqual(self.call("/api/admin/users/add", {"email": "z@x.com"}, headers=member)[0], 403)
        code, body = self.call("/api/admin/users/add", {"email": "New@X.com"})
        self.assertEqual(code, 200)
        self.assertIn("new@x.com", [u["email"] for u in body["users"]])
        self.assertEqual(self.call("/api/admin/users/add", {"email": "nope"})[0], 400)

    def test_users_only_see_their_own_status_for_a_shared_posting(self):
        other = db.users_add("member@x.com")["id"]
        jid = self.job()
        db.analysis_ensure(other, jid)
        code, body = self.call("/api/jobs/list")
        self.assertEqual(body["jobs"][0]["status"], "none")            # the admin has no row for it
        code, body = self.call("/api/jobs/list", headers=cookie("member@x.com"))
        self.assertEqual(body["jobs"][0]["status"], "pending")

    # ── manual jobs ──────────────────────────────────────────────────────
    def test_manual_add_creates_a_manual_posting_and_queues_it_at_high_priority(self):
        auto = self.job("auto")
        db.analysis_ensure(self.uid, auto)
        worker.enqueue(self.uid, auto, manual=False)
        code, body = self.call("/api/jobs/manual", {"company": "Zed", "title": "Backend Engineer",
                                                    "text": "Go role", "location": "Bengaluru"})
        self.assertEqual((code, body["is_new"], body["queued"]), (200, True, True))
        job = db.jobs_get(body["job_id"])
        self.assertEqual(job["source"], "manual")
        self.assertEqual(db.analysis_get(self.uid, body["job_id"])["status"], "in_process")
        first = worker._queue.get()                                      # the manual one jumps the line
        self.assertEqual((first[0], first[3]), (worker.MANUAL, body["job_id"]))

    def test_manual_add_twice_is_one_posting_and_one_queue_entry(self):
        payload = {"company": "Zed", "title": "Backend Engineer", "text": "Go role"}
        _, first = self.call("/api/jobs/manual", payload)
        _, second = self.call("/api/jobs/manual", payload)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertFalse(second["is_new"])
        self.assertFalse(second["queued"])
        self.assertEqual(worker._queue.qsize(), 1)
        self.assertEqual(db._one("SELECT COUNT(*) n FROM jobs")["n"], 1)

    def test_manual_add_validation(self):
        self.assertEqual(self.call("/api/jobs/manual", {"company": "A", "title": "B"})[0], 400)
        code, body = self.call("/api/jobs/manual", {"company": "A", "title": "x" * 201, "text": "t"})
        self.assertEqual(code, 400)
        self.assertIn("title", body["error"])
        self.assertEqual(db._one("SELECT COUNT(*) n FROM jobs")["n"], 0)

    def test_a_manual_job_with_a_non_engineering_title_still_shows(self):
        self.call("/api/jobs/manual", {"company": "A", "title": "Chief Wizard", "text": "t"})
        _, body = self.call("/api/jobs/list?eng_only=1&level=all")
        self.assertEqual(body["total_matched"], 1)

    def test_bad_json_is_a_400_not_a_dropped_connection(self):
        req = urllib.request.Request(self.base + "/api/jobs/manual", data=b"{not json", headers=self.admin)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            OPENER.open(req)
        self.assertEqual(cm.exception.code, 400)

    # ── regenerate / clear: only status + files ──────────────────────────
    def test_regenerate_resets_to_pending_deletes_files_and_requeues_at_priority(self):
        jid = self.job()
        folder = self.make_done(jid)
        code, _ = self.call("/api/jobs/regenerate", {"job_id": jid})
        self.assertEqual(code, 200)
        self.assertFalse(os.path.exists(folder))
        a = db.analysis_get(self.uid, jid)
        self.assertEqual((a["status"], a["attempts"], a["resume_path"], a["score"]), ("in_process", 1, None, None))
        self.assertEqual(a["requirements"], [])
        self.assertEqual(worker._queue.get()[0], worker.MANUAL)

    def test_clear_wipes_results_and_files_and_queues_nothing(self):
        jid = self.job()
        folder = self.make_done(jid)
        self.assertEqual(self.call("/api/jobs/clear", {"job_id": jid})[0], 200)
        self.assertFalse(os.path.exists(folder))
        a = db.analysis_get(self.uid, jid)
        self.assertEqual((a["status"], a["resume_path"], a["score"]), ("cleared", None, None))
        self.assertTrue(worker._queue.empty())
        self.assertEqual(db.analysis_pending(), [])                      # the poller won't pick it up either

    def test_clear_and_regenerate_are_refused_while_the_job_is_running(self):
        jid = self.job()
        db.analysis_ensure(self.uid, jid)
        db.analysis_claim(self.uid, jid)                                 # in_process
        for path in ("/api/jobs/regenerate", "/api/jobs/clear"):
            self.assertEqual(self.call(path, {"job_id": jid})[0], 409, path)
        self.assertEqual(db.analysis_get(self.uid, jid)["status"], "in_process")

    def test_regenerate_on_a_posting_with_no_row_yet_creates_one(self):
        jid = self.job()
        self.assertEqual(self.call("/api/jobs/regenerate", {"job_id": jid})[0], 200)
        self.assertIsNotNone(db.analysis_get(self.uid, jid))

    def test_regenerate_unknown_job_is_404_and_bad_id_is_400(self):
        self.assertEqual(self.call("/api/jobs/regenerate", {"job_id": 9999})[0], 404)
        self.assertEqual(self.call("/api/jobs/regenerate", {"job_id": "abc"})[0], 400)

    def test_one_users_clear_does_not_touch_anothers_files(self):
        other = db.users_add("member@x.com")["id"]
        jid = self.job()
        mine = self.make_done(jid)
        theirs = os.path.join(pipeline.FILES_DIR, str(other), "acme", str(jid))
        os.makedirs(theirs)
        open(os.path.join(theirs, "resume.pdf"), "wb").write(b"x")
        db.analysis_ensure(other, jid)
        self.call("/api/jobs/clear", {"job_id": jid})
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.exists(theirs))

    # ── status visible to the UI ─────────────────────────────────────────
    def test_list_and_detail_expose_status_attempts_and_error(self):
        ok, bad, fresh = self.job("1"), self.job("2"), self.job("3")
        self.make_done(ok)
        db.analysis_ensure(self.uid, bad)
        db.analysis_claim(self.uid, bad)
        db.analysis_start(self.uid, bad)
        db.analysis_fail(self.uid, bad, "timeout", "timeout", permanent=True)
        db.analysis_ensure(self.uid, fresh)
        _, body = self.call("/api/jobs/list")
        by_id = {j["id"]: j for j in body["jobs"]}
        self.assertEqual(by_id[ok]["status"], "done")
        self.assertEqual(by_id[fresh]["status"], "pending")
        self.assertEqual((by_id[bad]["status"], by_id[bad]["error_kind"], by_id[bad]["error_msg"]),
                         ("failed", "timeout", "timeout"))
        _, d = self.call(f"/api/jobs/detail?id={bad}")
        self.assertEqual(d["analysis"]["status"], "failed")

    def test_list_can_be_filtered_by_status(self):
        done, pend, none = self.job("1"), self.job("2"), self.job("3")
        self.make_done(done)
        db.analysis_ensure(self.uid, pend)
        for status, expect in (("done", {done}), ("pending", {pend}), ("none", {none})):
            _, body = self.call(f"/api/jobs/list?status={status}")
            self.assertEqual({j["id"] for j in body["jobs"]}, expect, status)

    def test_listing_shows_when_each_posting_was_fetched(self):
        jid = self.job()
        _, body = self.call("/api/jobs/list")
        job = body["jobs"][0]
        self.assertAlmostEqual(job["first_seen_ts"], time.time(), delta=30)
        first = job["first_seen_ts"]
        time.sleep(0.02)
        db.jobs_add("greenhouse", "1", "Acme", "Senior Software Engineer", "https://x", "Python role", "Bengaluru, India")
        _, body = self.call("/api/jobs/list")
        self.assertEqual(body["jobs"][0]["first_seen_ts"], first)        # re-fetching doesn't move it
        self.assertGreater(body["jobs"][0]["last_seen_ts"], first)

    def test_fetch_status_reports_queue_and_fetch_state(self):
        _, body = self.call("/api/jobs/fetch-status")
        self.assertEqual(set(body), {"queued", "current", "fetch"})

    # ── downloads ────────────────────────────────────────────────────────
    def test_download_serves_the_users_own_generated_files(self):
        jid = self.job()
        self.make_done(jid)
        for kind, name in server.DOWNLOAD_FILES.items():
            code, raw = self.call(f"/api/jobs/download?job_id={jid}&kind={kind}")
            self.assertEqual((code, raw), (200, b"content-of-" + name.encode()), kind)

    def test_download_before_done_or_for_a_failed_job_is_404(self):
        jid = self.job()
        self.assertEqual(self.call(f"/api/jobs/download?job_id={jid}&kind=resume")[0], 404)
        db.analysis_ensure(self.uid, jid)
        self.assertEqual(self.call(f"/api/jobs/download?job_id={jid}&kind=resume")[0], 404)

    def test_download_never_serves_another_users_files(self):
        other = db.users_add("member@x.com")["id"]
        jid = self.job()
        self.make_done(jid)                                              # the admin's
        code, _ = self.call(f"/api/jobs/download?job_id={jid}&kind=resume", headers=cookie("member@x.com"))
        self.assertEqual(code, 404)                                      # the member has no analysis of their own
        self.assertEqual(self.call(f"/api/jobs/download?job_id={jid}&kind=resume", headers={"X": "1"})[0], 401)

    def test_download_rejects_unknown_kinds_and_path_tricks(self):
        jid = self.job()
        self.make_done(jid)
        self.assertEqual(self.call(f"/api/jobs/download?job_id={jid}&kind=../../etc/passwd")[0], 400)
        self.assertEqual(self.call(f"/api/jobs/download?job_id={jid}&kind=resume%00")[0], 400)

    def test_download_refuses_a_stored_path_that_escapes_the_files_dir(self):
        jid = self.job()
        self.make_done(jid)
        outside = os.path.join(db.DATA_DIR, "secret")                # a real file, inside data/ but not under files/
        os.makedirs(outside, exist_ok=True)
        open(os.path.join(outside, "resume.pdf"), "wb").write(b"private")
        db._run("UPDATE analysis SET resume_path = 'secret/resume.pdf' WHERE user_id = ? AND job_id = ?", self.uid, jid)
        code, raw = self.call(f"/api/jobs/download?job_id={jid}&kind=resume")
        self.assertEqual(code, 404)
        self.assertNotEqual(raw, b"private")

    def test_signed_link_works_without_login_and_only_for_what_it_signs(self):
        jid = self.job()
        self.make_done(jid)
        url = links.download_url("", self.uid, jid, "resume")
        code, raw = self.call(url, headers={})
        self.assertEqual((code, raw), (200, b"content-of-resume.pdf"))
        self.assertEqual(self.call(url.replace("kind=resume", "kind=cover"), headers={})[0], 403)   # other file
        self.assertEqual(self.call(url.replace(f"job_id={jid}", f"job_id={jid + 1}"), headers={})[0], 403)
        self.assertEqual(self.call(url.replace(f"user={self.uid}", f"user={self.uid + 1}"), headers={})[0], 403)
        self.assertEqual(self.call(url[:-3] + "000", headers={})[0], 403)                            # bad signature

    # ── gaps tab ─────────────────────────────────────────────────────────
    def test_gaps_tab_reads_the_cache_and_only_refresh_calls_the_llm(self):
        calls = []
        real = llm.llm_json_call

        def fake(system, messages, tuning, **kw):
            calls.append(tuning)
            return json.dumps({"clusters": [{"label": "No Go", "job_ids": [self.jid]}]}), "zai"
        self.jid = self.job()
        self.make_done(self.jid)
        db._run("INSERT INTO analysis_gap (user_id, job_id, pos, gap, why, example) VALUES (?,?,0,'No Go','','')",
                self.uid, self.jid)
        llm.llm_json_call = fake
        try:
            _, first = self.call("/api/jobs/missing-clusters")
            _, again = self.call("/api/jobs/missing-clusters")
            self.assertEqual(calls, ["gap_cluster"])                     # the second open was served from the cache
            self.assertEqual(first["clusters"][0]["label"], "No Go")
            self.assertEqual(again["scored_count"], 1)
            self.call("/api/jobs/missing-clusters?refresh=1")
            self.assertEqual(calls, ["gap_cluster", "gap_cluster"])
        finally:
            llm.llm_json_call = real

    # ── applications ─────────────────────────────────────────────────────
    def test_mark_applied_is_idempotent_and_leaves_the_postings_list(self):
        jid = self.job()
        _, a = self.call("/api/jobs/mark-applied", {"job_id": jid, "via": "linkedin"})
        _, b = self.call("/api/jobs/mark-applied", {"job_id": jid, "via": "referral"})
        self.assertEqual(a["application_id"], b["application_id"])
        _, apps = self.call("/api/applications/list")
        self.assertEqual(len(apps["applications"]), 1)
        self.assertEqual(apps["applications"][0]["applied_via"], "linkedin")
        _, body = self.call("/api/jobs/list")
        self.assertEqual(body["total_matched"], 0)

    def test_status_events_append_in_order_and_only_to_your_own_application(self):
        other = db.users_add("member@x.com")["id"]
        jid = self.job()
        _, a = self.call("/api/jobs/mark-applied", {"job_id": jid})
        for status in ("phone_screen", "interview"):
            self.assertEqual(self.call("/api/applications/add-status", {"app_id": a["application_id"], "status": status})[0], 200)
        app = self.call("/api/applications/list")[1]["applications"][0]
        self.assertEqual([e["status"] for e in app["events"]], ["applied", "phone_screen", "interview"])
        self.assertEqual(app["status"], "interview")
        db.users_add("member@x.com")
        code, _ = self.call("/api/applications/add-status", {"app_id": a["application_id"], "status": "offer"},
                            headers=cookie("member@x.com"))
        self.assertEqual(code, 400)

    # ── static files ─────────────────────────────────────────────────────
    def test_static_serving_cannot_escape_the_static_directory(self):
        self.assertEqual(self.call("/jobs/")[0], 200)
        self.assertEqual(self.call("/jobs/../server.py")[0], 404)
        self.assertEqual(self.call("/jobs/..%2fserver.py")[0], 404)
        self.assertEqual(self.call("/jobs/../static-evil/x")[0], 404)


class LinksTest(unittest.TestCase):
    def test_signature_covers_user_job_kind_and_expiry(self):
        url = links.download_url("http://h", 3, 7, "resume")
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        self.assertTrue(links.verify(3, 7, "resume", q["exp"], q["sig"]))
        self.assertFalse(links.verify(4, 7, "resume", q["exp"], q["sig"]))
        self.assertFalse(links.verify(3, 8, "resume", q["exp"], q["sig"]))
        self.assertFalse(links.verify(3, 7, "cover", q["exp"], q["sig"]))
        self.assertFalse(links.verify(3, 7, "resume", int(q["exp"]) + 1, q["sig"]))

    def test_link_expires(self):
        exp = int(time.time()) - 1
        self.assertFalse(links.verify(3, 7, "resume", exp, links._sig(3, 7, "resume", exp)))
        self.assertFalse(links.verify(3, 7, "resume", "soon", "x"))

    def test_link_lasts_about_a_week(self):
        q = dict(p.split("=") for p in links.download_url("", 1, 1, "resume").split("?")[1].split("&"))
        self.assertAlmostEqual(int(q["exp"]) - time.time(), 7 * 86400, delta=60)


class SpecConstantsTest(unittest.TestCase):
    """The numbers the design was specified with."""

    def test_a_listing_gets_at_most_four_minutes_of_llm_and_render_time(self):
        self.assertEqual(pipeline.BUDGET_SECONDS, 240)

    def test_three_attempts_then_failed(self):
        self.assertEqual(db.MAX_ATTEMPTS, 3)

    def test_an_llm_call_is_capped_by_what_is_left_of_the_budget_and_refused_once_spent(self):
        llm.set_budget(30)
        try:
            self.assertLessEqual(llm._allowance(65), 30)              # asked for 65s, only ~30s left
            self.assertFalse(llm.budget_exceeded())
        finally:
            llm.clear_budget()
        llm.set_budget(-1)
        try:
            self.assertIsNone(llm._allowance(65))                     # spent: the call must not start
            self.assertTrue(llm.budget_exceeded())                    # and it is reported as a timeout
        finally:
            llm.clear_budget()

    def test_no_output_token_cap_was_added(self):
        import inspect
        self.assertEqual(inspect.signature(llm.llm_json_call).parameters["max_tokens"].default, 32000)

    def test_the_server_never_starts_a_second_worker_or_uses_locks(self):
        src = open(os.path.join(os.path.dirname(__file__), "worker.py")).read()
        self.assertEqual(src.count("Thread(target=_worker_loop"), 1)
        for banned in ("Semaphore", "ThreadPoolExecutor", "multiprocessing"):
            self.assertNotIn(banned, src)
        self.assertNotIn("Lock(", open(os.path.join(os.path.dirname(__file__), "pipeline.py")).read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
