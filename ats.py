"""Classifiers over a posting's title and location: is it an engineering role,
how senior, is it in the target location -- plus the one writing check the
master-document editor uses (does a bullet carry a brag)."""

import re


# Previously a DENYLIST (title is "engineering" unless it contains one of
# ~30 known non-eng phrases) -- replaced after a real audit found 30+ junk
# titles slipping through it unnoticed across the live fetched data: Credit
# Risk Operations Associate, Accounts Payable Analyst, Chief of Staff,
# Executive Business Partner, Legal Entity Controller, Real Estate Analyst,
# Fraud Operations Team Lead, India Tax Lead, and "ML Annotation QA Engineer"
# (contains "Engineer" but is a data-labeling QA role, not software) among
# many others -- a denylist can only ever block phrasings someone already
# thought to list, and the real world has far more non-engineering job
# families than anyone will enumerate. Flipped to an ALLOWLIST instead: a
# title has to actually look like a real software/data/ML/infra engineering
# role (the actual target), not merely avoid looking like something else.
#
# Two rounds of this still leaked (QA Engineer - Endpoint; AI GTM Engineer,
# a marketing-ops role that only matched via a sloppy "AI within 20 chars of
# Engineer" proximity pattern; Associate Lead Data Scientist; Applied AI
# Architect, a customer-facing deployment role -- all confirmed by reading
# the real posting body, not title alone). The proximity patterns are gone,
# QA/Data-Scientist patterns are gone, and "architect" is now only allowed
# after software/backend/frontend/fullstack (not bare ai/ml/data + architect,
# which was pulling in consulting-flavored roles). "Applied AI" is denied
# outright: checked 4 real "Applied AI Engineer/Architect" postings across
# OpenAI/Anthropic and 3 of 4 were partner/customer-facing (only "GTM Growth
# Engineering" was genuine hands-on SWE) -- title text alone can't tell
# those apart (same prefix, different team mission in the JD body), so this
# defaults to excluding the whole family rather than risk more of the
# partner-facing majority.
#
# This is a structural limit, not something more regex tuning fixes: a
# title-only classifier cannot always know a role's real function when two
# teams at the same company use near-identical title phrasing for different
# jobs. Verified against all 6,252 distinct titles in the live fetched set --
# every title below was read, not sampled, for the final round; "IT Controls
# Data Engineer" / "IT Software Engineer, Infrastructure" / "Sr. IT Site
# Reliability Software Engineer" remain medium-confidence (plausible real
# infra roles, not individually read against their JD body) rather than
# fully verified.
_ENG_TITLE_ALLOW = [
    re.compile(r"\bsoftware\s+(development\s+)?engineer", re.I),
    re.compile(r"\bengineer,?\s*software\b", re.I),
    re.compile(r"\bsoftware\s+developer\b", re.I),
    re.compile(r"\bsoftware\s+architect\b", re.I),
    re.compile(r"\bsde[\s-]?(1|2|3|i|ii|iii)?\b", re.I),
    re.compile(r"\bswe\b", re.I),   # common abbreviation (Meta, Pinterest, ...), same as SDE/SDET above
    re.compile(r"\bsdet\b", re.I),
    re.compile(r"\b(backend|frontend|front-end|back-end|full[\s-]?stack)\s+(software\s+)?(engineer|developer|architect)\b", re.I),
    re.compile(r"\bengineer\b.*\b(backend|frontend|full[\s-]?stack)\b", re.I),
    re.compile(r"\b(data|ml|machine learning|ai|platform|infrastructure|infra|site reliability|sre|devops|dev ops|release|security|cloud)\s+(software\s+)?engineer\b", re.I),
    re.compile(r"\bmember\s+of\s+technical\s+staff\b", re.I),
    re.compile(r"\bmts\b", re.I),
    re.compile(r"\bmachine\s+learning\s+engineer\b", re.I),
    re.compile(r"\bapplication(s)?\s+engineer\b", re.I),
    re.compile(r"\bsite\s+reliability\s+engineer\b", re.I),
    re.compile(r"\bdevops\s+engineer\b", re.I),
    re.compile(r"\brelease\s+engineer\b", re.I),
    re.compile(r"\bsecurity\s+engineer\b", re.I),
    re.compile(r"\bsoftware\s+engineer\s+in\s+test\b", re.I),
    # language-named developer/engineer roles, independent of company -- a
    # real miss found in the audit ("Principal Java Developer - Distributed
    # Systems, Serverless" at Elasticsearch didn't contain the word
    # "software" at all).
    re.compile(r"\b(java|python|golang|go|c\+\+|c#|ruby|scala|kotlin|rust|node(\.js)?)\s+(developer|engineer)\b", re.I),
]

# Still needed even with an allowlist: a few title families would otherwise
# match an allow pattern above for the wrong reason -- "Forward Deployed
# [Software] Engineer" and "Solutions Engineer" both legitimately contain
# "engineer" next to real tech words, but are customer-facing/pre-sales
# roles, not the hands-on-building target; "ML Annotation QA Engineer"
# matched an earlier looser pattern but is data-labeling QA, not software;
# "Partner Applied AI Engineer"/"IT Service Engineer" are partner-facing and
# internal-corporate-IT roles respectively, confirmed by reading real
# postings (see the long comment above _ENG_TITLE_ALLOW).
NON_ENGINEERING_TITLE_WORDS = {
    "intern", "internship", "co-op",
    "solutions architect", "solution architect", "solutions engineer", "solution engineer",
    "sales engineer", "support engineer", "support developer",
    "forward deployed", "field engineer", "field engineering",
    "annotation", "qa engineer", "quality engineer", "data scientist",
    "applied ai",
    "partner engineer", "partner platform engineer",
    "it service engineer", "it systems engineer", "it software architect",
    "it support engineer",
}
# Word-boundary regex, not a plain substring check -- found live: "intern"
# as a bare substring was also matching "Internal", "Internals", and
# "International" (18 real engineering postings wrongly excluded this way,
# e.g. "Senior Software Engineer, Fullstack (International)" and "Software
# Engineer - Database Engine Internals"), and "quality engineer" was
# matching inside "Quality Engineering" used as a department name after a
# comma, not the actual title. \b on both ends of the whole phrase fixes
# both classes at once -- "engineer" directly followed by "ing" has no
# boundary between them, so it stops matching there too.
_NON_ENG_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(NON_ENGINEERING_TITLE_WORDS, key=len, reverse=True)) + r")\b",
    re.I)


def is_likely_engineering_title(title):
    if _NON_ENG_RE.search(title):
        return False
    return any(p.search(title) for p in _ENG_TITLE_ALLOW)


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


# India/Bangalore/Bengaluru is the only target, not a toggle -- fetch_ats.py
# already enforces this at fetch time (a non-India posting is never saved),
# this is the same check applied again at read time. \b word-boundary match
# -- a plain substring check matched "Indiana" on "india", a real false
# positive found in the fetched data. Moved here (was in platform/server.py)
# so db.py can register it as a SQL function -- same reasoning as
# title_seniority_tier/is_likely_engineering_title living here: it's job-row
# classification logic, not web-server logic, and SQL-level filtering needs
# to call it per-row without a circular import back into the server module.
_INDIA_WORDS = ("india", "bangalore", "bengaluru")
_INDIA_RE = re.compile(r"\b(" + "|".join(_INDIA_WORDS) + r")\b")


def is_target_location(location, description_raw):
    loc = (location or "").lower()
    if loc:
        return bool(_INDIA_RE.search(loc))
    return bool(_INDIA_RE.search((description_raw or "")[:500].lower()))


def is_eligible(title, location, description_raw):
    """A posting worth generating a resume for: engineering title, not a
    manager-tier role, in the target location. Applied when analysis rows
    are created, so ineligible postings never enter the queue."""
    return (is_likely_engineering_title(title) and title_seniority_tier(title) != "manager"
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
