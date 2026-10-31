"""Tests for master.py: the master document is one JSON file per user, edited
only by the user. Run: python3 test_master.py"""
import json
import os
import shutil
import tempfile
import threading
import unittest

_tmp = tempfile.mkdtemp()
os.environ["JOBSEARCH_DATA"] = _tmp
os.environ["ALLOWED_LOGIN_EMAIL"] = "admin@x.com"

import db  # noqa: E402
import master  # noqa: E402


class MasterTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(master._dir(), ignore_errors=True)
        self.a, self.b = 1, 2

    def read_file(self, uid):
        with open(master.path(uid)) as f:
            return json.load(f)

    def test_a_user_with_no_file_has_an_empty_document(self):
        self.assertEqual((master.profile_all(self.a), master.skills_all(self.a), master.point_bank_all(self.a)), ({}, [], []))
        self.assertFalse(os.path.exists(master.path(self.a)))            # reading never creates a file

    def test_it_lives_in_one_readable_json_file_named_by_user_id(self):
        master.profile_set(self.a, "name", "Ada")
        master.skills_add(self.a, "Python", "Languages")
        pid = master.point_bank_add(self.a, "Acme", "Engineer", "2020-01", None, "Cut latency by 40%.", tags=["perf"])
        self.assertEqual(os.path.basename(master.path(self.a)), "1.json")
        doc = self.read_file(self.a)
        self.assertEqual(doc["profile"], {"name": "Ada"})
        self.assertEqual(doc["skills"], [{"name": "Python", "category": "Languages", "active": True}])
        self.assertEqual(doc["points"][0]["id"], pid)
        self.assertEqual(doc["points"][0]["text"], "Cut latency by 40%.")

    def test_editing_the_file_by_hand_takes_effect_immediately(self):
        master.profile_set(self.a, "name", "Ada")
        doc = self.read_file(self.a)
        doc["profile"]["name"] = "Ada Lovelace"
        with open(master.path(self.a), "w") as f:
            json.dump(doc, f)
        self.assertEqual(master.profile_all(self.a)["name"], "Ada Lovelace")

    def test_users_have_separate_documents(self):
        master.profile_set(self.a, "name", "Ada")
        master.point_bank_add(self.a, "Acme", "Eng", "2020", None, "a thing")
        self.assertEqual(master.profile_all(self.b), {})
        self.assertEqual(master.point_bank_all(self.b), [])
        master.delete(self.a)
        self.assertEqual(master.profile_all(self.a), {})
        master.delete(self.a)                                            # deleting twice is fine

    def test_skills_are_case_insensitive_and_re_adding_updates_and_reactivates(self):
        master.skills_add(self.a, "Embeddings", "ML")
        master.skills_add(self.a, "embeddings", "AI")
        self.assertEqual([(s["name"], s["category"]) for s in master.skills_all(self.a)], [("Embeddings", "AI")])
        with self.assertRaises(ValueError):
            master.skills_add(self.a, "  ", "x")

    def test_point_ids_increase_and_are_never_reused_after_retiring(self):
        first = master.point_bank_add(self.a, "Acme", "Eng", "2020", None, "one")
        second = master.point_bank_add(self.a, "Acme", "Eng", "2020", None, "two")
        master.point_bank_set_active(self.a, second, False)
        third = master.point_bank_add(self.a, "Acme", "Eng", "2020", None, "three")
        self.assertEqual((first, second, third), (1, 2, 3))

    def test_retire_restore_and_edit_text(self):
        pid = master.point_bank_add(self.a, "Acme", "Eng", "2020", None, "old text", tags=["Go", "go", "sql", " "])
        self.assertEqual(master.point_bank_all(self.a)[0]["tags"], ["Go", "go", "sql"])
        master.point_bank_set_active(self.a, pid, False)
        self.assertEqual(master.point_bank_all(self.a), [])
        self.assertEqual(len(master.point_bank_all(self.a, active_only=False)), 1)
        master.point_bank_set_active(self.a, pid, True)
        master.point_bank_update_text(self.a, pid, "  new text  ")
        self.assertEqual(master.point_bank_all(self.a)[0]["text"], "new text")

    def test_unknown_point_and_empty_text_are_rejected(self):
        with self.assertRaises(ValueError):
            master.point_bank_update_text(self.a, 99, "x")
        with self.assertRaises(ValueError):
            master.point_bank_set_active(self.a, 99, False)
        pid = master.point_bank_add(self.a, "Acme", "Eng", "2020", None, "fine")
        with self.assertRaises(ValueError):
            master.point_bank_update_text(self.a, pid, "   ")
        self.assertEqual(master.point_bank_all(self.a)[0]["text"], "fine")

    def test_points_are_listed_newest_start_first_then_in_the_order_added(self):
        master.point_bank_add(self.a, "Old", "Eng", "2018-01", "2019-01", "old")
        x = master.point_bank_add(self.a, "New", "Eng", "2024-04", None, "new one")
        y = master.point_bank_add(self.a, "New", "Eng", "2024-04", None, "new two")
        master.point_bank_add(self.a, "Mid", "Eng", "2024", "2024", "mid")
        self.assertEqual([p["text"] for p in master.point_bank_all(self.a)], ["new one", "new two", "mid", "old"])
        self.assertEqual((x, y), (2, 3))

    def test_a_bad_document_is_refused_and_the_old_file_survives(self):
        master.profile_set(self.a, "name", "Ada")
        before = self.read_file(self.a)
        bad_docs = [
            {"profile": {"name": 5}},
            {"profile": {}, "skills": [{"name": "Go"}]},
            {"profile": {}, "skills": [{"name": "Go", "category": "x"}, {"name": "go", "category": "y"}]},
            {"profile": {}, "points": [{"id": 1, "entry_type": "role", "company": "A", "role": "r", "text": "t"},
                                        {"id": 1, "entry_type": "role", "company": "A", "role": "r", "text": "t"}],
             "next_point_id": 5},
            {"profile": {}, "points": [{"id": 1, "entry_type": "hobby", "company": "A", "role": "r", "text": "t"}],
             "next_point_id": 2},
            {"profile": {}, "points": [{"id": 1, "entry_type": "role", "company": "A", "role": "r", "text": ""}],
             "next_point_id": 2},
            {"profile": {}, "points": [{"id": 4, "entry_type": "role", "company": "A", "role": "r", "text": "t"}],
             "next_point_id": 4},
            [],
        ]
        for doc in bad_docs:
            with self.assertRaises(ValueError, msg=doc):
                master.save(self.a, doc)
        self.assertEqual(self.read_file(self.a), before)
        self.assertEqual([f for f in os.listdir(master._dir()) if f.startswith(".tmp")], [])

    def test_a_hand_edited_file_that_is_broken_fails_loudly_naming_the_file(self):
        master.profile_set(self.a, "name", "Ada")
        with open(master.path(self.a), "w") as f:
            f.write("{ nope")
        with self.assertRaises(ValueError) as cm:
            master.profile_all(self.a)
        self.assertIn("1.json", str(cm.exception))

    def test_a_failed_write_never_leaves_a_half_written_file(self):
        master.profile_set(self.a, "name", "Ada")
        real = os.replace
        os.replace = lambda *a: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                master.profile_set(self.a, "name", "Changed")
        finally:
            os.replace = real
        self.assertEqual(master.profile_all(self.a)["name"], "Ada")
        self.assertEqual([f for f in os.listdir(master._dir()) if f.startswith(".tmp")], [])

    def test_simultaneous_edits_do_not_lose_each_other(self):
        ids, errors = [], []

        def add(i):
            try:
                ids.append(master.point_bank_add(self.a, "Acme", "Eng", "2020", None, f"point {i}"))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=add, args=(i,)) for i in range(25)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ids), list(range(1, 26)))
        self.assertEqual(len(master.point_bank_all(self.a)), 25)

    def test_nothing_in_the_master_document_path_calls_an_llm(self):
        src = open(os.path.join(os.path.dirname(__file__), "master.py")).read()
        self.assertNotIn("import llm", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
