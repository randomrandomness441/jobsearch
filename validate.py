"""Deterministic, after-the-fact checklist for every rule we actually ask the
Z.ai calls to follow when generating a resume/cover letter: reworded (not
copy-pasted), plain language, exactly 10 role bullets split 4-6/5-5/6-4,
exactly one project (Kapitsa only) when one is present, one page, tense
matches real employment dates, no new numbers that aren't in the point bank.

None of this calls an LLM -- it's the same "the machine is the arbiter"
principle check_human_style() already uses for style. Built because asking
"did the model actually do X" and trusting the prompt's wording was no
longer good enough once a real generation (1) included two Kapitsa rows
when the rule is exactly one, and (2) used present tense for a role that
had already ended -- both only caught by eyeballing the PDF, which doesn't
scale and isn't a real check. generate_cli.py calls build_checklist() right
after generate.generate() and writes the result to validation_checklist.md
next to the resume, so every generation is self-auditing instead of trusted
on faith.
"""
import difflib
import re
from datetime import date

import humanizer

_NUM_RE = re.compile(r"\d[\d,]*\.?\d*\s*[kKmMbB]?\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SUFFIX_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

MIN_BULLETS_PER_COMPANY = 4
MAX_BULLETS_PER_COMPANY = 6
TARGET_TOTAL_ROLE_BULLETS = 10

# Present-tense first-person verbs HUMAN_STYLE/the draft prompt are told to
# only use for a role with no real end_date (still ongoing). "I'm building"
# and "currently" are the two phrasings that actually showed up live.
_PRESENT_TENSE_RE = re.compile(
    r"\bi(?:'m| am)?\s+(build|own|lead|manage|drive|run|maintain)\b|\bcurrently\b", re.I)


def _norm(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def _numbers_in(text):
    """Normalizes "7K" and "7,000" to the same token so a paraphrase that
    spells out a K/M/B suffix isn't mistaken for a new, fabricated number --
    found live: "7K queries per week" (original) vs "7,000 requests per
    week" (reworded) flagged as fabrication before this normalization."""
    nums = set()
    for m in _NUM_RE.finditer(text):
        tok = m.group().strip()
        suffix = tok[-1].lower() if tok and tok[-1].lower() in _SUFFIX_MULT else ""
        digits = (tok[:-1] if suffix else tok).replace(",", "").strip()
        if not digits:
            continue
        try:
            val = float(digits) * _SUFFIX_MULT.get(suffix, 1)
        except ValueError:
            continue
        nums.add(str(int(val)) if val == int(val) else str(val))
    return nums


def _check_reworded(priority_items, point_bank_by_id):
    """Flags any selected bullet whose final text is identical, or
    near-identical, to the real point-bank row it came from. NOT
    automatically a bug: the selection prompt's own rule is "reword only if
    it echoes raw JD vocabulary (CHECK A) or reads jargon-heavy (CHECK B) --
    if a bullet already passes both, leaving it untouched is correct". This
    is a visibility check (which rows were left verbatim, so a human can
    spot-check whether that was the right call), not a hard pass/fail on
    its own -- a high flagged-count is the real signal something's off."""
    copy_pasted = []
    for item in priority_items:
        row = point_bank_by_id.get(item["row_id"])
        if row is None:
            continue
        a, b = _norm(item["text"]), _norm(row["text"])
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio > 0.92:
            copy_pasted.append((item["row_id"], round(ratio, 2)))
    ok = not copy_pasted
    detail = ("every selected bullet differs meaningfully from its point-bank original" if ok
              else "left verbatim/near-identical (only correct if it already read plainly with "
                   "no JD jargon to begin with -- spot-check these): " +
                   ", ".join(f"row {rid} ({ratio*100:.0f}% match)" for rid, ratio in copy_pasted))
    return {"check": "Bullets reworded, not copy-pasted", "ok": ok, "detail": detail}


def _check_style_gate(priority_items, summary, cover_letter_body):
    """Re-runs the same deterministic style gate generate() already enforces
    on summary/cover letter, but ALSO against each individual selected
    bullet -- defense in depth, since a bullet's text is assembled from
    several retry/salvage paths in _zai_select_content and this is the one
    place that re-checks the text that actually ends up on the page."""
    violations = []
    for item in priority_items:
        for v in humanizer.check_human_style(item["text"]):
            violations.append(f"row {item['row_id']}: {v}")
    for label, text in (("summary", summary), ("cover letter", cover_letter_body)):
        for v in humanizer.check_human_style(text):
            violations.append(f"{label}: {v}")
    ok = not violations
    detail = "clean -- no banned words/phrases, em dashes, run-ons, or non-English text" if ok \
        else "; ".join(violations)
    return {"check": "Plain-language style gate", "ok": ok, "detail": detail}


def _check_evidence_survives_trim(requirements, final_row_ids):
    """A requirement call marked itself satisfied by pointing at a specific
    point-bank row (evidence_row_id). If the one-page trim later cut that
    exact row, the resume no longer actually shows what the score claims
    it shows -- this is the one place that fact gets checked instead of
    assumed."""
    broken = []
    for req in requirements or []:
        rid = req.get("evidence_row_id")
        if rid is not None and req.get("presence") in ("yes", "semantic") and rid not in final_row_ids:
            broken.append(f"\"{req.get('requirement', '')}\" cited row {rid}, which isn't on the final resume")
    ok = not broken
    detail = "every claimed requirement's evidence row is actually present on the resume" if ok \
        else "; ".join(broken)
    return {"check": "Claimed JD evidence survives the page trim", "ok": ok, "detail": detail}


def _check_bullet_split(roles):
    counts = {r["company"]: len(r["_row_ids"]) for r in roles}
    total = sum(counts.values())
    split_str = ", ".join(f"{c}={n}" for c, n in counts.items())
    in_range = all(MIN_BULLETS_PER_COMPANY <= n <= MAX_BULLETS_PER_COMPANY for n in counts.values())
    ok = total == TARGET_TOTAL_ROLE_BULLETS and in_range
    if ok:
        detail = f"{total} role bullets ({split_str}) -- matches the 4-6/5-5/6-4 rule"
    else:
        reason = "under the 10-bullet target" if total < TARGET_TOTAL_ROLE_BULLETS else \
                 "over the 10-bullet target" if total > TARGET_TOTAL_ROLE_BULLETS else \
                 "a company is outside the 4-6 floor/cap"
        detail = f"{total} role bullets ({split_str}) -- {reason} (page/content trim can legitimately cause this; not a style-gate failure)"
    return {"check": "10 role bullets, split 4-6/5-5/6-4", "ok": ok, "detail": detail}


def _check_single_project(projects):
    names = {p["name"] for p in projects}
    ok = len(projects) <= 1 and names <= {"Kapitsa"}
    if not projects:
        detail = "no project on this resume"
    elif ok:
        detail = f"exactly one project ({next(iter(names))})"
    else:
        detail = f"{len(projects)} project(s) present: {', '.join(names)} -- rule is exactly one, Kapitsa only"
    return {"check": "At most one project, Kapitsa only", "ok": ok, "detail": detail}


def _check_one_page(pages, max_pages):
    ok = pages is not None and pages <= max_pages
    detail = f"{pages} page(s)" if pages is not None else "page count unknown (render failed)"
    return {"check": f"Fits in {max_pages} page", "ok": ok, "detail": detail}


def _company_end_dates(point_bank_items):
    ends = {}
    for row in point_bank_items:
        if row.get("entry_type") != "role":
            continue
        company = row["company"]
        # A company is "ongoing" if ANY of its rows has no end_date -- a single
        # missing end_date means the role itself hasn't ended, regardless of
        # what other rows for the same company say.
        if not row.get("end_date"):
            ends[company] = None
        elif company not in ends:
            ends[company] = row["end_date"]
    return ends


def _check_tense(summary, cover_letter_body, point_bank_items, today=None):
    today = today or date.today()
    company_end = _company_end_dates(point_bank_items)
    ended_companies = []
    for company, end_date in company_end.items():
        if end_date:
            try:
                y, m = (int(x) for x in end_date.split("-")[:2])
                if date(y, m, 1) <= today:
                    ended_companies.append(company)
            except (ValueError, IndexError):
                continue
    flagged = []
    for label, text in (("summary", summary), ("cover letter", cover_letter_body)):
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            for company in ended_companies:
                if company.lower() in sentence.lower() and _PRESENT_TENSE_RE.search(sentence):
                    flagged.append(f'{label}: present tense used for {company} (ended {company_end[company]}): "{sentence.strip()[:80]}..."')
    ok = not flagged
    detail = "no present-tense claims for roles that have already ended" if ok else "; ".join(flagged)
    return {"check": "Tense matches real employment dates", "ok": ok, "detail": detail}


def _check_no_new_numbers(priority_items, point_bank_by_id):
    """A reworded bullet introducing a number that isn't anywhere in its
    point-bank original is exactly the failure mode from the 15-40 -> 3000
    QPS incident -- catch it mechanically instead of relying on someone
    eyeballing every regenerate."""
    flagged = []
    for item in priority_items:
        row = point_bank_by_id.get(item["row_id"])
        if row is None:
            continue
        new_nums = _numbers_in(item["text"])
        orig_nums = _numbers_in(row["text"])
        extra = {n for n in new_nums if n not in orig_nums}
        if extra:
            flagged.append(f"row {item['row_id']}: new number(s) {sorted(extra)} not in the original text")
    ok = not flagged
    detail = "every number in a reworded bullet also appears in its point-bank original" if ok \
        else "; ".join(flagged)
    return {"check": "No fabricated numbers", "ok": ok, "detail": detail}


def build_checklist(priority_items, roles, projects, summary, cover_letter_body,
                     point_bank_items, pages, requirements=None, max_pages=1, today=None):
    """Returns a list of {"check", "ok", "detail"} dicts, most-fundamental
    first. priority_items: selection["priority_items"] (pre-trim, what the
    LLM actually selected/reworded). roles/projects: generate.generate()'s
    OWN return values (post-trim -- what's actually on the final page)."""
    point_bank_by_id = {row["id"]: row for row in point_bank_items}
    final_row_ids = {rid for r in roles for rid in r["_row_ids"]} | {rid for p in projects for rid in p["_row_ids"]}
    return [
        _check_reworded(priority_items, point_bank_by_id),
        _check_no_new_numbers(priority_items, point_bank_by_id),
        _check_style_gate(priority_items, summary, cover_letter_body),
        _check_tense(summary, cover_letter_body, point_bank_items, today=today),
        _check_bullet_split(roles),
        _check_single_project(projects),
        _check_one_page(pages, max_pages),
        _check_evidence_survives_trim(requirements, final_row_ids),
    ]


def render_checklist_md(checklist):
    passed = sum(1 for c in checklist if c["ok"])
    lines = [f"# Validation checklist: {passed}/{len(checklist)} passed\n"]
    for c in checklist:
        mark = "PASS" if c["ok"] else "FAIL"
        lines.append(f"- [{mark}] {c['check']}\n  {c['detail']}")
    return "\n".join(lines) + "\n"
