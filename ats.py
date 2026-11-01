"""Classifiers over a posting's title and location: is it a title we apply for,
how senior, is it in a wanted location -- plus the one writing check the
master-document editor uses (does a bullet carry a brag)."""

import re


# The titles we apply for. A title must contain one of these phrases:
#   software engineer   (so also Senior / Staff / Software Engineer 2 / II)
#   SDE 1, 2 or 3       (SDE-2, SDE2, SDE II, SDE III ...)
#   backend engineer    (also Back-end / Back End)
#   data engineer
#   member of technical staff, or MTS
# Years of experience in the description are deliberately not looked at.
_TARGET_TITLE_PATTERNS = [
    re.compile(r"\bsoftware\s+engineer\b", re.I),
    re.compile(r"\bback[\s-]?end\s+engineer\b", re.I),
    re.compile(r"\bdata\s+engineer\b", re.I),
    re.compile(r"\bsde[\s-]?(1|2|3|iii|ii|i)\b", re.I),
    re.compile(r"\bmember\s+of\s+technical\s+staff\b", re.I),
    re.compile(r"\bmts\b", re.I),
]

# Phrases that match above but aren't the role: internships, and the
# customer-facing "forward deployed" / "applied AI" families.
_EXCLUDED_TITLE_RE = re.compile(r"\b(?:intern|internship|co-op|forward\s+deployed|applied\s+ai)\b", re.I)


def is_target_title(title):
    if _EXCLUDED_TITLE_RE.search(title):
        return False
    return any(p.search(title) for p in _TARGET_TITLE_PATTERNS)


def title_seniority_tier(title):
    """Rough seniority tier from the title alone. Complements (doesn't
    replace) years_required extracted from the JD body -- some postings
    never state years explicitly, but the title still names the tier
    plainly ("Staff", "Senior Staff", "Manager"). Order matters: check the
    more specific "senior staff" before the bare "staff" it contains."""
    low = title.lower()
    if re.search(r"\b(engineering\s+)?manager\b|\bdirector\b|head of engineering|\bvp\b|vice president", low):
        return "manager"
    if re.search(r"senior staff|sr\.?\s*staff", low):
        return "senior_staff"
    if re.search(r"\bstaff\b|\bprincipal\b", low):
        return "staff"
    if re.search(r"\bsenior\b|\bsr\.?\b|\bsde[\s-]?(iii|3)\b", low):
        return "senior"
    return "mid"


# Where we look: Bengaluru / Bangalore, or anything marked Remote. \b word
# boundaries, so "Remotely" or a place that merely contains one of these words
# doesn't match. A posting with no location at all is judged on the first 500
# characters of its description instead.
_LOCATION_WORDS = ("remote", "bengaluru", "bangalore")
_LOCATION_RE = re.compile(r"\b(" + "|".join(_LOCATION_WORDS) + r")\b", re.I)


def is_target_location(location, description_raw=""):
    text = (location or "").strip() or (description_raw or "")[:500]
    return bool(_LOCATION_RE.search(text))


def is_wanted(title, location, description_raw=""):
    """The one check a posting must pass to be stored at all, run at fetch
    time: a target title, not a manager-tier role, in a wanted
    location. (Age is the other fetch-time check; see fetch_ats.py.)"""
    return (is_target_title(title) and title_seniority_tier(title) != "manager"
            and is_target_location(location, description_raw))


# Real, specific resume-bullet rubric Sourav gave: "every bullet needs to be
# a keyword and/or a brag... if it doesn't have a keyword and/or a brag, it
# shouldn't exist." Keyword = a term from the JD's own qualifications/must-
# haves. Brag = a quantified claim, OR -- when there's no hard number -- an
# award, feedback, or named result. Checked against the real point bank
# before building this: 14 of 15 real bullets already pass (11 have a
# number, 3 more have a clear outcome clause with no digit --
# "cutting infrastructure cost", "keeping bounce/complaint rates under
# thresholds"); exactly one fails ("Reviewed code... ran regular technical
# sessions" -- pure activity, no keyword, no brag, no result).
#
# A hard filter in generate.py now, not just a soft flag: automated (Zai-
# drafted) generation has no per-job human glance before the resume renders,
# so a bullet that fails this can't ride through on the assumption someone
# will catch it. Whether text "has a brag" is still a real judgment call
# (the 3 no-digit examples above are genuine brags a strict digit-only
# check would wrongly reject) -- the brag-word list is deliberately generous
# to keep the false-exclude rate low, since silently dropping a real
# accomplishment is worse than keeping one weak bullet.
_BRAG_WORDS = re.compile(
    r"\b(cut|cutting|reduce[ds]?|reducing|increase[ds]?|increasing|improve[ds]?|improving|"
    r"prevent(?:ed|s|ing)?|flagged|caught|kept|keeping|letting|enabl(?:ed|ing|es)|"
    r"first|sole|only|led|owned|praised|award(?:ed)?|promoted|saved|savings|"
    r"scal(?:ed|ing)|automat(?:ed|ing))\b", re.I)
_HAS_NUMBER = re.compile(r"\d")

# Rule 4: "no generic verbs like 'Implemented X' with nothing else -- name
# what you designed, optimized, or solved." A bullet opening on one of these
# with no brag signal anywhere in it reads as a bare responsibility, not an
# accomplishment -- checked as a weak OPENER specifically (not banned
# outright), since "Helped design and ship..." followed by a real result is
# fine; it's the bare, brag-free version these catch.
_WEAK_OPENERS = re.compile(
    r"^(implemented|worked on|helped with|responsible for|involved in|assisted with)\b", re.I)


def check_bullet_brag(bullet_text):
    """The half of the rubric that's a property of the bullet's own
    writing, independent of any job posting: does it have a brag (number
    or result word), and does it avoid opening on a bare generic verb with
    nothing else backing it. Unlike "has a keyword" (which depends on what
    a specific JD asks for and so can only be judged per job), this half
    is exactly as true or false the day the point bank entry is written --
    meant to be checked THEN, at the master-doc source, not discovered
    later when a bullet quietly gets cut from some future resume. Returns
    {"has_brag": bool, "weak_opener": bool, "ok": bool}."""
    has_brag = bool(_HAS_NUMBER.search(bullet_text)) or bool(_BRAG_WORDS.search(bullet_text))
    weak_opener = bool(_WEAK_OPENERS.match(bullet_text.strip())) and not has_brag
    return {"has_brag": has_brag, "weak_opener": weak_opener, "ok": has_brag and not weak_opener}
