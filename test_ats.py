"""One test per title case for ats.is_target_title / ats.is_wanted: the title
must contain "software engineer", "backend engineer", "data engineer", "SDE 1/2/3",
"member of technical staff" or "MTS"; years of experience in the description
play no part.
Run: python3 test_ats.py"""
import os
import re
import tempfile
import unittest

os.environ["JOBSEARCH_DATA"] = tempfile.mkdtemp()
os.environ["ALLOWED_LOGIN_EMAIL"] = "admin@x.com"

import ats  # noqa: E402

ACCEPT = [
    # software engineer, and the levels that contain it
    "Software Engineer",
    "software engineer",
    "SOFTWARE ENGINEER",
    "Senior Software Engineer",
    "Sr. Software Engineer",
    "Staff Software Engineer",
    "Software Engineer 2",
    "Software Engineer II",
    "Software Engineer - II",
    "Software Engineer, Backend",
    "Backend Software Engineer",
    "Software Engineer (Remote)",
    # backend engineer
    "Backend Engineer",
    "backend engineer",
    "Back-end Engineer",
    "Back End Engineer",
    "Senior Backend Engineer",
    "Staff Backend Engineer",
    "Backend Engineer II",
    "Backend Engineer, Payments",
    # data engineer
    "Data Engineer",
    "data engineer",
    "Senior Data Engineer",
    "Data Engineer 2",
    "Data Engineer, Platform",
    "Lead Data Engineer",
    # software engineer at every level number
    "Software Engineer 1",
    "Software Engineer I",
    "Software Engineer 3",
    "Software Engineer III",
    "Software Engineer - III",
    # SDE 1, 2, 3
    "SDE 1",
    "SDE-1",
    "SDE1",
    "SDE I",
    "SDE 3",
    "SDE-3",
    "SDE3",
    "SDE III",
    "sde iii",
    "Senior SDE 3",
    "SDE 3 - Platform",
    "SDE 2",
    "SDE-2",
    "SDE2",
    "SDE II",
    "sde ii",
    "Senior SDE 2",
    "SDE 2 - Platform",
    # member of technical staff / MTS
    "Member of Technical Staff",
    "member of technical staff",
    "Senior Member of Technical Staff",
    "Member of Technical Staff, Backend",
    "MTS",
    "MTS 2",
    "MTS-2",
    "Senior MTS",
]

REJECT = [
    # engineer titles that are not one of the phrases
    "Frontend Engineer",
    "Front-end Engineer",
    "Fullstack Engineer",
    "Machine Learning Engineer",
    "Data Scientist",
    "Data Analyst",
    "Data Engineering Manager",            # "engineering", and a manager
    "Backend Developer",
    "Site Reliability Engineer",
    "DevOps Engineer",
    "Platform Engineer",
    "Security Engineer",
    "Software Developer",
    "Software Architect",
    "Software Engineering Lead",           # "engineering", not "engineer"
    # SDE with no level, or a level beyond 3, was not asked for
    "SDE",
    "SDE 4",
    "SDE IV",
    "SDE Intern",
    "Sdeveloper",
    # near misses on the short words
    "MTSU Coordinator",
    "Smts Analyst",
    "Product Designer",
    "Account Executive",
    "Solutions Engineer",
    "Sales Engineer",
    "Support Engineer",
    # matches a phrase but is not the role
    "Software Engineer Intern",
    "Software Engineer, Internship",
    "Software Engineer (Co-op)",
    "Forward Deployed Software Engineer",
    "Applied AI Software Engineer",
    "Backend Engineer Intern",
    "Data Engineer Intern",
    "Data Engineer (Co-op)",
    "Member of Technical Staff Intern",
]

# match a phrase, rejected only because they manage people
MANAGERS = [
    "Software Engineering Manager",     # not a phrase match either way, kept here as a guard
    "Software Engineer Manager",
    "Manager, Software Engineer",
    "Director, Software Engineer",
    "VP, Software Engineer",
    "Member of Technical Staff Manager",
    "SDE 2 Manager",
    "SDE 3 Manager",
    "Manager, SDE 1",
    "Backend Engineer Manager",
    "Manager, Backend Engineer",
    "Data Engineer Manager",
    "Director, Data Engineer",
]


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "empty"


class TitleTests(unittest.TestCase):
    pass


def _add(cls, name, fn):
    setattr(cls, name, fn)


for _title in ACCEPT:
    _add(TitleTests, f"test_accepts_{_slug(_title)}",
         (lambda t: lambda self: self.assertTrue(ats.is_target_title(t), t))(_title))
for _title in REJECT:
    _add(TitleTests, f"test_rejects_{_slug(_title)}",
         (lambda t: lambda self: self.assertFalse(ats.is_target_title(t), t))(_title))
for _title in MANAGERS:
    _add(TitleTests, f"test_wanted_rejects_manager_{_slug(_title)}",
         (lambda t: lambda self: self.assertFalse(ats.is_wanted(t, "Bengaluru"), t))(_title))


class WantedTests(unittest.TestCase):
    def test_years_of_experience_in_the_description_are_ignored(self):
        for years in ("2-4 years", "5+ years", "6+ years of experience", "10+ years required", "no years mentioned"):
            self.assertTrue(ats.is_wanted("Software Engineer", "Bengaluru", f"We need {years}."), years)

    def test_a_target_title_in_a_wrong_location_is_not_wanted(self):
        self.assertFalse(ats.is_wanted("Software Engineer", "Austin, TX"))
        self.assertFalse(ats.is_wanted("Software Engineer", "Hyderabad, India"))

    def test_a_wrong_title_in_a_wanted_location_is_not_wanted(self):
        self.assertFalse(ats.is_wanted("Frontend Engineer", "Bengaluru"))
        self.assertFalse(ats.is_wanted("Machine Learning Engineer", "Remote"))

    def test_wanted_needs_all_three(self):
        self.assertTrue(ats.is_wanted("Senior Software Engineer", "Remote - India"))
        self.assertTrue(ats.is_wanted("SDE 2", "Bangalore"))
        self.assertTrue(ats.is_wanted("Member of Technical Staff", "Remote"))
        self.assertTrue(ats.is_wanted("Backend Engineer", "Bengaluru, India"))
        self.assertTrue(ats.is_wanted("Data Engineer", "Remote - India"))

    def test_the_sql_title_filter_uses_the_same_rule(self):
        import db
        con = db.connect()
        try:
            for title in ACCEPT + REJECT:
                got = con.execute("SELECT IS_TARGET_TITLE(?)", (title,)).fetchone()[0]
                self.assertEqual(got, 1 if ats.is_target_title(title) else 0, title)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=1)
