"""End-to-end tests for pipeline.process_one: a real docx/soffice render, a
throwaway database and data directory, and the LLM replaced by a scripted
fake. Run: python3 test_pipeline.py"""
import json
import os
import re
import stat
import tempfile
import time
import unittest

_tmp = tempfile.mkdtemp()
os.environ["JOBSEARCH_DATA"] = _tmp
os.environ["ALLOWED_LOGIN_EMAIL"] = "admin@x.com"

import db  # noqa: E402
import llm  # noqa: E402
import pipeline  # noqa: E402

PROFILE = {"name": "Test Person", "title": "Software Engineer", "email": "t@x.com", "phone": "555-0100",
           "linkedin": "linkedin.com/in/test", "github": "github.com/test", "education": "BSc, Somewhere",
           "years_experience": "5"}
BULLETS = ["Built a payments service that handled 2 million requests a day.",
           "Cut query time by 40 percent by adding a covering index.",
           "Moved the batch jobs to a queue and removed the nightly outage."]


class Fake:
    """Scripted llm.llm_json_call. mode: ok | none | timeout."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def __call__(self, system, messages, tuning, max_tokens=0, temperature=0):
        self.calls.append(tuning)
        if self.mode == "timeout":
            llm._local.exceeded = True
            return None, None
        if self.mode == "none" or (self.mode == "draft_none" and tuning == "draft"):
            return None, None
        llm._record({"prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 30, "cached_tokens": 10})
        llm._local.provider = "zai"
        ids = [int(i) for i in re.findall(r"\[id=(\d+) \|", messages[0]["content"])]
        if tuning == "score":
            return json.dumps({
                "requirements": [
                    {"requirement": "Python", "importance": "required", "presence": "yes", "depth": "quantified",
                     "evidence_item_id": ids[0], "weight": 70, "earned": 70},
                    {"requirement": "Go", "importance": "BOGUS", "presence": "no", "depth": "n/a",
                     "evidence_item_id": 99999, "weight": 30, "earned": 0}],
                "project_assessment": [], "missing": [{"gap": "No Go", "why": "none", "example": "ship a service"}],
                "reason": "Strong on the main requirement."}), "zai"
        if tuning == "select":
            return json.dumps({
                "role_assessment": [{"item_id": i, "worth_including": True, "reason": "relevant",
                                     "text": BULLETS[n % 3]} for n, i in enumerate(ids)],
                "forced_project_items": [], "skills": [{"name": "Python", "display": "Python"}]}), "zai"
        return json.dumps({
            "summary": "I have five years of experience building backend systems. At Acme I ran a service at 2 million requests a day.",
            "cover_letter_body": ("I want to work on your platform team.\n\nAt Acme I built a payments service. It handled "
                                  "2 million requests a day.\n\nI would like to bring that to your team.")}), "zai"


class PipelineTest(unittest.TestCase):
    def setUp(self):
        for t in ("analysis", "jobs", "point_bank", "skills", "profile", "users"):
            db._run(f"DELETE FROM {t}" + (" WHERE role != 'admin'" if t == "users" else ""))
        self.uid = db.users_get("admin@x.com")["id"]
        for k, v in PROFILE.items():
            db.profile_set(self.uid, k, v)
        db.skills_add(self.uid, "Python", "Languages")
        for b in BULLETS:
            db.point_bank_add(self.uid, "Acme", "Engineer", "2020", None, b)
        self.job_id, _ = db.jobs_add("greenhouse", "42", "Acme", "Senior Software Engineer", "https://x/42",
                                     "Python backend role in Bengaluru.", "Bengaluru, India")
        db.analysis_ensure(self.uid, self.job_id)
        self._real = llm.llm_json_call
        self._budget = pipeline.BUDGET_SECONDS

    def tearDown(self):
        llm.llm_json_call = self._real
        pipeline.BUDGET_SECONDS = self._budget

    def run_job(self, mode="ok"):
        llm.llm_json_call = self.fake = Fake(mode)
        self.assertTrue(db.analysis_claim(self.uid, self.job_id))
        return pipeline.process_one(self.uid, self.job_id)

    def test_success_saves_files_details_and_metrics(self):
        self.assertEqual(self.run_job(), "done")
        a = db.analysis_get(self.uid, self.job_id)
        self.assertEqual((a["status"], a["score"], a["provider"], a["attempts"]), ("done", 70, "zai", 1))
        for rel in (a["resume_path"], a["cover_path"]):
            self.assertTrue(os.path.isfile(os.path.join(db.DATA_DIR, rel)), rel)
        self.assertEqual(os.path.dirname(a["resume_path"]), f"files/{self.uid}/acme/{self.job_id}")
        self.assertEqual(a["requirements"][1]["importance"], "required")           # BOGUS coerced
        self.assertIsNone(a["requirements"][1]["evidence_point_id"])               # unknown id nulled
        self.assertEqual([g["gap"] for g in a["gaps"]], ["No Go"])
        self.assertEqual([s["step"] for s in a["steps"]], ["score", "select", "draft", "render"])
        self.assertTrue(all(s["ok"] and s["duration_ms"] >= 0 for s in a["steps"]))
        score_step = next(s for s in a["steps"] if s["step"] == "score")
        self.assertEqual((score_step["provider"], score_step["prompt_tokens"], score_step["cached_tokens"]),
                         ("zai", 100, 10))
        self.assertEqual(self.fake.calls, ["score", "select", "draft"])
        self.assertFalse([d for d in os.listdir(pipeline.FILES_DIR) if d.startswith("tmp-")])   # temp dir cleaned
        items = db._all("SELECT * FROM analysis_item WHERE user_id = ? AND job_id = ?", self.uid, self.job_id)
        self.assertEqual(len(items), 3)

    def test_llm_failure_retries_then_fails_with_reason(self):
        self.assertEqual(self.run_job("none"), "retry")
        self.assertEqual(db.analysis_get(self.uid, self.job_id)["status"], "retry")
        self.assertEqual(self.run_job("none"), "retry")
        self.assertEqual(self.run_job("none"), "failed")
        a = db.analysis_get(self.uid, self.job_id)
        self.assertEqual((a["status"], a["error_kind"], a["attempts"]), ("failed", "llm", 3))
        self.assertIn("score", a["error_msg"])
        self.assertEqual(sum(1 for s in a["steps"] if not s["ok"]), 3)             # each attempt left a failed step row
        self.assertIsNone(a["resume_path"])

    def test_timeout_is_recorded_as_just_timeout(self):
        self.assertEqual(self.run_job("timeout"), "retry")
        a = db.analysis_get(self.uid, self.job_id)
        self.assertEqual((a["error_kind"], a["error_msg"]), ("timeout", "timeout"))

    def test_a_later_step_failing_keeps_earlier_step_metrics(self):
        self.assertEqual(self.run_job("draft_none"), "retry")
        a = db.analysis_get(self.uid, self.job_id)
        self.assertEqual({(s["step"], s["ok"]) for s in a["steps"]}, {("score", 1), ("select", 1), ("draft", 0)})

    def test_missing_profile_fails_immediately_without_retries(self):
        db._run("DELETE FROM profile WHERE user_id = ? AND key = 'phone'", self.uid)
        self.assertEqual(self.run_job(), "failed")
        a = db.analysis_get(self.uid, self.job_id)
        self.assertEqual((a["error_kind"], a["attempts"]), ("invalid", 1))
        self.assertIn("phone", a["error_msg"])
        self.assertEqual(self.fake.calls, [])                                       # never reached the LLM

    def test_empty_master_document_fails_immediately(self):
        db._run("UPDATE point_bank SET active = 0 WHERE user_id = ?", self.uid)
        self.assertEqual(self.run_job(), "failed")
        self.assertIn("master document", db.analysis_get(self.uid, self.job_id)["error_msg"])

    def test_render_overrun_kills_the_whole_process_group(self):
        marker = os.path.join(_tmp, "child.pid")
        script = os.path.join(_tmp, "slow.sh")
        with open(script, "w") as f:      # a "render" that spawns a grandchild and then hangs
            f.write(f"#!/bin/sh\nsleep 60 &\necho $! > {marker}\nsleep 60\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        real_venv, real_min, real_remaining = pipeline.VENV_PY, pipeline.MIN_RENDER_SECONDS, llm.remaining
        pipeline.VENV_PY, pipeline.MIN_RENDER_SECONDS = script, 0
        llm.remaining = lambda: 1.5
        try:
            started = time.time()
            self.assertEqual(self.run_job(), "retry")
        finally:
            pipeline.VENV_PY, pipeline.MIN_RENDER_SECONDS, llm.remaining = real_venv, real_min, real_remaining
        self.assertLess(time.time() - started, 15)
        a = db.analysis_get(self.uid, self.job_id)
        self.assertEqual((a["error_kind"], a["error_msg"]), ("timeout", "timeout"))
        time.sleep(0.3)
        with open(marker) as f:
            pid = int(f.read())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)                                                         # the grandchild is dead too

    def test_regenerate_replaces_files_and_never_leaves_a_gap(self):
        self.assertEqual(self.run_job(), "done")
        first = db.analysis_get(self.uid, self.job_id)["resume_path"]
        ok, old = db.analysis_regenerate(self.uid, self.job_id)
        self.assertTrue(ok)
        pipeline.remove_files(old)
        self.assertFalse(os.path.exists(os.path.join(db.DATA_DIR, first)))
        self.assertEqual(self.run_job(), "done")
        self.assertTrue(os.path.isfile(os.path.join(db.DATA_DIR, db.analysis_get(self.uid, self.job_id)["resume_path"])))

    def test_failed_rerun_keeps_previous_files(self):
        self.assertEqual(self.run_job(), "done")
        path = os.path.join(db.DATA_DIR, db.analysis_get(self.uid, self.job_id)["resume_path"])
        db._run("UPDATE analysis SET status = 'retry', attempts = 1 WHERE user_id = ? AND job_id = ?", self.uid, self.job_id)
        self.assertEqual(self.run_job("none"), "retry")
        self.assertTrue(os.path.isfile(path))                                        # untouched by the failed run

    def test_remove_files_refuses_paths_outside_the_files_dir(self):
        victim = os.path.join(_tmp, "keep")
        os.makedirs(victim)
        pipeline.remove_files(["../keep/x.pdf", "../../etc/passwd"])
        self.assertTrue(os.path.isdir(victim))

    def test_slugify_is_bounded_and_path_safe(self):
        s = pipeline.slugify("../../Acme Corp / Ünïcode " + "x" * 300)
        self.assertLessEqual(len(s), 60)
        self.assertRegex(s, r"^[a-z0-9-]+$")
        self.assertEqual(pipeline.slugify("///"), "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
