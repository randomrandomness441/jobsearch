"""The LLM helpers behind the Profile and Gaps tabs: rewrite one bullet, tidy a
company's bullets, extract points from an uploaded resume, cluster the gaps
across postings. None of them writes to the database -- each returns a
proposal for the server to show the user or save."""

import json
import re
import subprocess
import sys

import llm
from pipeline import VENV_PY, trim_text


def rewrite_point(raw_text, company, role):
    """Reformats ONE point-bank bullet into Keyword+Use+Result shape --
    never invents. If a real brag/result is already present just phrased
    badly, rewrite it; if it genuinely isn't there, refuse and say exactly
    what's missing instead of making one up.

    Returns a dict: {"status": "rewritten", "text": str} or
    {"status": "missing_info", "explanation": str, "question": str}, or
    None if the Zai call itself failed (caller should treat that as "try
    again later", not as a real refusal)."""
    system = (
        "You reformat ONE real resume bullet point into a stronger shape, without ever "
        "inventing anything. Never add a fact, number, outcome, or detail that isn't "
        "literally present in the text given to you -- if you're tempted to guess a number "
        "or soften a gap with a vague phrase, stop and refuse instead.\n\n"
        "The target shape: name the real skill/technology, how it was actually used, and "
        "the measurable result or named outcome -- one bullet, one or two sentences, no "
        "paragraph.\n\n"
        "If the given text already contains a real result (a number, a named outcome, an "
        "award, concrete feedback) but is phrased awkwardly or buries it, rewrite it into "
        "that shape, preserving every real fact and number exactly as given.\n\n"
        "If the given text has NO real result or outcome anywhere in it -- a bare activity "
        "or responsibility with nothing measurable or named -- do NOT invent one. Instead "
        "explain in one sentence what's missing, then ask ONE specific, concrete question "
        "that would help recall the real missing fact (a number, a before/after, who "
        "noticed, what changed).\n\n"
        "No AI-sounding buzzwords (delve, leverage, underscore, boast, robust, seamless), "
        "no em dashes.\n\n"
        "Reply with ONLY a JSON object, one of:\n"
        '{"status": "rewritten", "text": "<the rewritten bullet>"}\n'
        '{"status": "missing_info", "explanation": "<one sentence, what\'s missing>", '
        '"question": "<one specific question to help recall it>"}'
    )
    user = f"Company/role: {company} / {role}\n\nBullet as written:\n{raw_text}"
    reply, provider = llm.llm_json_call(system, [{"role": "user", "content": user}], "rewrite_point", max_tokens=32000, temperature=0.3)
    if reply is None:
        return None
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        if obj.get("status") == "rewritten":
            return {"status": "rewritten", "text": str(obj["text"]).strip()}
        return {"status": "missing_info", "explanation": str(obj.get("explanation", "")).strip(),
                "question": str(obj.get("question", "")).strip()}
    except Exception as e:
        print(f"[jobs] {provider} point-rewrite parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return None


# Used by the on-demand "Tidy master doc" button: given everything
# already on file for ONE company, return the cleaned-up complete list --
# same real accomplishment told as 2+ points gets combined into one,
# carrying every real detail; genuinely distinct points come back
# untouched. The caller (see /api/profile/points/tidy) diffs the result
# against what's on file by plain text match -- the model only ever
# handles text in, text out, no ids to track or get wrong.
def tidy_company(company, role, existing_texts):
    """Returns {"points": [str, ...], "notes": [str, ...]} -- the complete
    point list for this company after cleanup, and anything worth flagging
    (e.g. a factual conflict between two merged points). None on a call/
    parse failure (caller treats that as 'couldn't check, leave as-is')."""
    items_text = "\n".join(existing_texts)
    system = (
        "You clean up a candidate's real accomplishment points for ONE company/role. You're given "
        "every point currently on file for it.\n\n"
        "Return the complete list of points for this company after cleanup: combine any two or more "
        "points that describe the SAME real accomplishment into one bullet carrying every real fact, "
        "number, and detail from all of them. Never invent anything not literally present in the "
        "input, and never drop a real detail just because only one of the combined points mentioned it.\n\n"
        "Two points are the same accomplishment if they're about the same system, project, or event -- "
        "even if worded completely differently. They are NOT the same just because they share a "
        "technology or domain (two different features of the same system are two different "
        "accomplishments, not one).\n\n"
        "If two points disagree on a number or fact for the same accomplishment, keep the more "
        "specific/complete one and say so in notes -- never silently drop the discrepancy.\n\n"
        "For any point that doesn't need combining with anything, return it completely unchanged, "
        "character for character -- don't reword, polish, or restructure something that isn't "
        "actually being merged.\n\n"
        "Any point you DO rewrite as part of a merge must follow this shape: name the real "
        "skill/technology, how it was actually used, and the measurable result -- one bullet, one or "
        "two sentences, no paragraph. No AI-sounding buzzwords (delve, leverage, underscore, boast, "
        "robust, seamless), no em dashes.\n\n"
        "Reply with ONLY a JSON object: {\"points\": [str, ...], \"notes\": [str, ...]} -- notes lists "
        "anything worth flagging (e.g. a conflict you resolved), empty if nothing to flag."
    )
    user = f"Company/role: {company} / {role}\n\nExisting points:\n{items_text}"
    reply, provider = llm.llm_json_call(system, [{"role": "user", "content": user}], "rewrite_point", max_tokens=32000, temperature=0.2)
    if reply is None:
        return None
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        return {"points": [str(t).strip() for t in obj.get("points", [])],
                "notes": [str(n).strip() for n in obj.get("notes", [])]}
    except Exception as e:
        print(f"[jobs] {provider} tidy parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return None


def cluster_gaps(gaps):
    """gaps: [{"job_id", "gap"}] -- every gap flagged against this user, free
    text from the scoring call. "No demonstrated Python depth" and "No production Python or Golang
    experience" are the same real gap, worded differently, and a plain
    string/keyword match won't see that. One LLM call groups the real,
    raw gaps (tagged with which posting each came from) into distinct
    underlying weaknesses -- semantically distinct, not textually distinct.
    Sorted by how many postings actually hit it, in Python (trust the
    count's arithmetic, not the model's), so the most-common real gap
    leads. Returns [] if there's nothing to cluster or the call failed --
    an empty tab is honest here, not an error."""
    raw = [{"job_id": r["job_id"], "gap": r["gap"].strip()} for r in gaps if r["gap"].strip()]
    if not raw:
        return []
    items_text = "\n".join(f"[job_id={r['job_id']}] {r['gap']}" for r in raw)
    system = (
        "You're given real skill/experience gaps flagged against a candidate across many real job "
        "postings, each tagged with which posting it came from. Group them into distinct underlying "
        "weaknesses -- gaps about the same real skill or experience area belong in the same group, "
        "even if worded completely differently (e.g. \"No demonstrated Python depth\" and \"No "
        "production Python or Golang experience\" are the SAME gap). Don't force genuinely different "
        "gaps together just because they're both vaguely technical (\"no Python\" and \"no Rust\" are "
        "different groups). For each group, give a short, clear, plain-English label naming the real "
        "gap -- not a copy of one entry's wording, a real synthesis -- and the job_ids of every "
        "posting that reported something in this group (a posting can only appear in a group once, "
        "even if it had multiple similar entries).\n\n"
        "Reply with ONLY a JSON object: {\"clusters\": [{\"label\": str, \"job_ids\": [int, ...]}]}."
    )
    user = f"Gaps:\n{items_text}"
    reply, provider = llm.llm_json_call(system, [{"role": "user", "content": user}], "gap_cluster", max_tokens=32000, temperature=0.1)
    if reply is None:
        return []
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        valid_ids = {r["job_id"] for r in raw}
        clusters = []
        for c in obj.get("clusters", []):
            label = str(c.get("label", "")).strip()
            job_ids = sorted({int(jid) for jid in c.get("job_ids", []) if int(jid) in valid_ids})
            if label and job_ids:
                clusters.append({"label": label, "count": len(job_ids), "job_ids": job_ids})
        clusters.sort(key=lambda c: c["count"], reverse=True)
        return clusters
    except Exception as e:
        print(f"[jobs] {provider} gap-clustering parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return []


def extract_file_text(path, ext):
    """PDF via pdftotext (system-wide), docx via this project's own venv
    (python-docx isn't installed for this process's plain python3).
    Returns "" on failure rather than raising -- an unreadable upload
    should surface as "found nothing", not crash the request."""
    try:
        if ext == ".pdf":
            out = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, timeout=30)
            return out.stdout
        if ext == ".docx":
            code = ("import sys\nfrom docx import Document\n"
                    "d = Document(sys.argv[1])\n"
                    "print('\\n'.join(p.text for p in d.paragraphs))")
            out = subprocess.run([VENV_PY, "-c", code, path],
                                  capture_output=True, text=True, timeout=30)
            return out.stdout
        if ext in (".txt", ".md"):
            with open(path, "r", errors="replace") as f:
                return f.read()
    except Exception as e:
        print(f"[profile] extract failed: {e}", file=sys.stderr)
    return ""


def extract_profile(resume_text, known_companies, existing_points_text):
    """Structured extraction: real resume/pasted text in, candidate
    point_bank rows + skills out -- grounded, never invents beyond what's
    literally in the text. Caller treats the result as a PROPOSAL for human
    review, never auto-committed, same discipline as everywhere else in
    this project.

    Also applies the same Keyword+Use+Result reshaping _zai_rewrite_point
    does for a single manually-typed bullet, in this SAME call instead of a
    second pass per point. A point with no real result anywhere in the
    source text is NOT invented a number -- it's extracted as written and
    flagged needs_review with what's missing.

    existing_points_text is shown to the model so it can skip proposing a
    point that just reworded something already on file -- resume variants
    kept re-extracting the same accomplishment with slightly different
    phrasing and it was landing in the point bank twice with no check at
    all. point_bank_find_similar (difflib, in db.py) is the deterministic
    safety net for near-verbatim repeats specifically."""
    system = (
        'Extract structured career facts from a resume or pasted text, and shape each one into a strong '
        'resume bullet. Rules: (1) never invent a fact, number, skill, or result not literally stated in '
        'the text -- if something is ambiguous, leave it out rather than guess. (2) One point_bank row per '
        'distinct accomplishment. (3) For each accomplishment, if the source text already contains a real '
        'result (a number, a named outcome, an award, concrete feedback) but states it plainly or buries '
        'it, reshape it into: the real skill/technology, how it was actually used, and that measurable '
        'result -- one bullet, one or two sentences, preserving every real fact and number exactly as '
        'given. No AI-sounding buzzwords (delve, leverage, underscore, boast, robust, seamless), no em '
        'dashes. (4) If an accomplishment has NO real result or outcome anywhere in the source text -- a '
        'bare activity or responsibility with nothing measurable or named -- do NOT invent one. Extract it '
        'as written (lightly cleaned up only), set needs_review=true, and give a one-sentence review_note '
        'explaining what\'s missing plus one specific question that would help recall the real fact. '
        '(5) Reuse company names exactly as they already appear in the candidate\'s known companies list '
        'if the resume clearly refers to the same employer. (6) Do NOT propose a point that describes the '
        'same underlying accomplishment as one already listed below, even if the wording differs -- skip '
        'it entirely rather than re-adding a reworded duplicate. Only propose points for facts/numbers not '
        'already covered.\n'
        'Reply with ONLY a JSON object: {"points": [{"company": str, "role": str, "start_date": '
        '"YYYY-MM or empty", "end_date": "YYYY-MM or empty (empty means current)", "text": str, '
        '"tags": [str, ...], "needs_review": bool, "review_note": "str or empty"}], '
        '"skills": [{"name": str, "category": str}]}.'
    )
    user = (f"Known companies already on file: {', '.join(known_companies) or '(none yet)'}\n\n"
            f"Accomplishments already in the point bank -- do not re-propose any of these, reworded or not:\n"
            f"{existing_points_text or '(none yet)'}\n\n"
            f"Resume text:\n{trim_text(resume_text, limit=15000)}")
    reply, provider = llm.llm_json_call(system, [{"role": "user", "content": user}], "extract_profile", max_tokens=32000, temperature=0.1)
    if reply is None:
        return None, "no LLM provider configured, or both Zai and Claude calls failed -- check server logs"
    try:
        m = re.search(r"\{.*\}", reply, re.S)
        obj = json.loads(m.group(0) if m else reply)
        return obj, None
    except Exception as e:
        return None, f"couldn't parse the model's response: {e}"
