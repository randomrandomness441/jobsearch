"""One job, start to finish: score -> select -> draft -> render -> save.

process_one(user_id, job_id) is the single function the worker runs, whether
the job came from the poller or from a manual click. It runs an analysis row
the caller has already claimed (db.analysis_claim) and leaves it done, retry
or failed -- nothing is left in_process.

    budget    the whole run gets BUDGET_SECONDS of wall clock, LLM calls and
              render together. Running out is recorded as a timeout.
    metrics   every step writes an analysis_step row (duration, provider,
              tokens) as it ends, so even a failed attempt leaves its timings.
    files     rendered into a temp directory and moved into place only after
              the render succeeded, so a failed or half-finished run never
              touches the files a previous run left behind.
    result    the details (requirements, gaps, projects, resume bullets) and
              the done status are saved in ONE transaction -- done is never
              visible without them.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

import db
import humanizer
import llm

ROOT = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.path.join(db.DATA_DIR, "files")
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python3")
BUDGET_SECONDS = 240
MIN_RENDER_SECONDS = 10      # don't start a render with less than this left
# What resume_render.py reads out of the profile unconditionally.
REQUIRED_PROFILE = ("name", "title", "email", "phone", "linkedin", "github", "education")

HUMAN_STYLE_RULES = """
How you talk (write like a person speaking, not like an AI assistant):
- One idea per sentence. If you catch yourself adding a parenthetical or a clause after
  a comma to sneak in a second idea, stop and make it its own sentence instead.
- Plain connecting words only: and, but, so, because. Never "furthermore", "moreover",
  "additionally".
- Never use an em dash. Use a period or a comma.
- Never write "it's not just X, it's Y".
- Never use "delve", "leverage", "underscore", "boast", "robust", "seamless".
- Say things directly. Don't hedge with "can be complex" or "it's worth noting that".
- Don't close with "let me know if you have any questions" or similar.
"""


JOBS_JD_CHAR_LIMIT = 12000          # covers all but the single longest fetched posting in full


def trim_text(text, limit=JOBS_JD_CHAR_LIMIT):
    """Truncate at a real word/line boundary, not mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary > limit * 0.9:   # don't backtrack far enough to lose a lot
        cut = cut[:boundary]
    return cut + "\n\n[...truncated]"


def score_requirements(job, profile, point_bank_items, candidate_years):
    """Call 1: scores this one JD against the candidate's real role + project items --
    requirements (the sole source of truth for `score`), project worth_including/reason,
    and gaps. Never picks resume content or words anything -- select_content (Call 2)
    takes this call's `requirements` as grounding so the two agree on what matters.

    point_bank_items: db.point_bank_all() rows (role and project items
    together, each with its real id/company/role/dates/entry_type/text).

    Same structured-rubric discipline the old combined call used
    (importance/presence/depth per requirement, real JSON not a vibe).

    Returns {"requirements", "missing", "project_assessment", "score",
    "reason"}, or None if the call/parse failed."""
    role_items_text = "\n".join(
        f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
        for p in point_bank_items if p["entry_type"] == "role" and not p["text"].lstrip().startswith("["))
    project_items_text = "\n".join(
        f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
        for p in point_bank_items if p["entry_type"] == "project" and not p["text"].lstrip().startswith("["))
    system = (
        "You score one real candidate's fit against one real job posting -- not a vague "
        "impression, and not by literal keyword-counting -- and separately flag real gaps. "
        "You're given the candidate's role items (real work-experience accomplishments) and, "
        "separately, their project items -- each with a stable id. Do three things:\n\n"
        "1. REQUIREMENTS -- identify the 5 to 8 requirements from the posting that actually matter "
        "(hard skills, tools, years of experience, domain knowledge). Judge every one of these by the "
        "real scope and complexity of what the candidate actually did -- NEVER by a job title or level "
        "label (\"SDE-1\", \"Senior\", \"Staff\", ...) sitting next to it. A first job is never going to "
        "carry a senior title; that's just how careers start, not a sign the work itself was junior. If "
        "a role item describes real ownership, scale, or architectural judgment, credit that regardless "
        "of what the company happened to call the position.\n"
        "   When one JD bullet names two or more distinct skills (\"X and Y\", \"X/Y\", \"X or Y\"), give "
        "EACH one its own requirement line -- never fold them into a single requirement just because the "
        "posting listed them together. Confirmed live: folding \"JavaScript/TypeScript and Python\" into "
        "one \"Python skills\" requirement let a candidate with zero TypeScript earn full credit for it, "
        "because the missing half was never its own line to score \"no\" on. If the candidate is strong "
        "on one named skill and has nothing on the other, that MUST show up as two requirements with two "
        "different presence values, not one requirement averaged into looking satisfied. If splitting "
        "every compound bullet this way would push past 8 requirements, keep the split and let the "
        "count run a little over rather than re-merging two distinct skills back into one line just to "
        "hit the range -- 5 to 8 is a guide for how many distinct things usually matter, not a hard "
        "ceiling that overrides giving a real, separately-missed skill its own honest score.\n"
        "   For each requirement, judge:\n"
        "   - importance: \"required\" or \"nice_to_have\" (infer from phrasing -- \"must\"/\"required\" "
        "vs \"a plus\"/\"bonus\" -- if the posting doesn't label sections).\n"
        "   - presence: \"yes\" (a role item demonstrates it, named or clearly-transferable work -- "
        "e.g. JD wants \"Kubernetes\", candidate ran \"container orchestration at scale\" using it), "
        "\"semantic\" (a different but equivalent term -- JD wants \"React\", candidate shows "
        "\"React.js\"), or \"no\". Never mark \"semantic\"/\"yes\" for something only vaguely related -- "
        "that's still \"no\".\n"
        "   - depth: \"quantified\" (a real number/scale/named outcome backs it), \"named\" (mentioned "
        "with no quantified backing), or \"n/a\" if presence is \"no\".\n"
        "   - evidence_item_id: the id of the single role item that best demonstrates this "
        "requirement, or null if presence is \"no\".\n"
        "   - weight: how many of 100 total points this requirement is worth. HARD RULE, not a "
        "preference: every \"nice_to_have\" requirement's weight COMBINED must sum to 20 or less out of "
        "the 100 -- the remaining 80+ belongs to \"required\" requirements, split between them. A bonus "
        "the candidate happens to be strong on must never be able to outweigh a stated requirement; this "
        "cap is what guarantees that mathematically, not just by intent. If the posting is unusually "
        "soft and you genuinely find zero \"required\" requirements, don't force the 20-point cap "
        "anyway -- in that specific case nice_to_have requirements may split the full 100 among "
        "themselves, since the cap's whole purpose is protecting required requirements from being "
        "outweighed, and there's nothing to protect once none exist. Every requirement's weight "
        "together must still sum to EXACTLY 100 -- divide it out deliberately, don't just guess numbers "
        "that happen to be close.\n"
        "   - earned: how many of those weight points this requirement actually earns -- full weight "
        "only for presence \"yes\" with depth \"quantified\"; real but partial credit for \"semantic\" "
        "or \"named\" (judge how close, don't default to a fixed fraction); 0 for \"no\". This is what "
        "makes the final score auditable -- be honest and consistent, since these numbers are shown to "
        "the candidate directly as exactly where their score is coming from.\n\n"
        "2. PROJECTS -- separately, for each project item, judge whether it's genuinely worth adding "
        "to this specific resume: real relevance to this JD, or real standalone strength, vs. thin or "
        "redundant with the role experience already shown. This is advisory only -- your "
        "worth_including/reason here is a judgment call for the record, not a final inclusion "
        "decision (a candidate can still choose to force a project onto the resume regardless of your "
        "verdict).\n\n"
        "3. GAPS -- identify real weaknesses relative to this JD: either a requirement area with "
        "nothing in the role items that could honestly speak to it at all, OR one where there's "
        "something but it reads thin or junior for what this JD actually wants (a bare mention, not "
        "the depth/scale/ownership the posting implies). For each one, give: the gap itself, why what's "
        "there (if anything) isn't enough, and one concrete, realistic example of the kind of "
        "accomplishment that would close it -- specific enough to act on (what it would involve, roughly "
        "what scale/result would make it count), not generic career advice. This is a real, useful "
        "signal, not a failure to paper over.\n\n"
        "Write a two-sentence reason (the real gap if there is one -- required vs nice-to-have, stated "
        "plainly -- or the strongest match if there isn't, then one concrete thing that would close the "
        "gap or strengthen it further). Never cite a job title, level label, or years-of-tenure-at-one-"
        "company as the gap by itself -- if tenure genuinely falls short of a stated bar, say that "
        "plainly as its own real gap, but never phrase a title (\"SDE-1\", \"too junior a role\") as if "
        "the title itself were the deficiency. The weight/earned numbers above already determine the "
        "score -- you don't compute it separately, just make sure your reason is consistent with what "
        "they show.\n\n"
        "Reply with ONLY a JSON object: {\"requirements\": [{\"requirement\": str, \"importance\": str, "
        "\"presence\": str, \"depth\": str, \"evidence_item_id\": int|null, \"weight\": int, "
        "\"earned\": int}], \"project_assessment\": [{\"item_id\": int, \"worth_including\": bool, "
        "\"reason\": str}], \"missing\": [{\"gap\": str, \"why\": str, \"example\": str}], "
        "\"reason\": str}."
    )
    user = (
        f"Candidate: {profile.get('title', '')}, {candidate_years} years of real experience.\n\n"
        f"Role items (id | company -- role, dates):\n{role_items_text}\n\n"
        f"Project items (id | project -- tagline, dates):\n{project_items_text or '(none in the master doc yet)'}\n\n"
        f"Job posting -- {job['title']} at {job['company']}:\n{trim_text(job['description_raw'])}"
    )
    messages = [{"role": "user", "content": user}]
    by_id = {p["id"]: p for p in point_bank_items}
    reply, provider = llm.llm_json_call(system, messages, "score", max_tokens=32000, temperature=0.2)
    if reply is None:
        return None
    try:
        m = re.search(r"\{.*\}", reply, re.S)   # tolerate stray text/fences around the JSON
        obj = json.loads(m.group(0) if m else reply)
        reason = str(obj.get("reason", ""))
        if len(reason) > 700:
            cut = reason[:700]
            boundary = max(cut.rfind(" "), cut.rfind("\n"))
            reason = cut[:boundary] if boundary > 700 * 0.9 else cut
        requirements = []
        total_earned = 0
        for r in obj.get("requirements", []):
            if not isinstance(r, dict):
                continue
            try:
                weight = max(0, int(r.get("weight", 0)))
                earned = max(0, min(weight, int(r.get("earned", 0))))
            except (TypeError, ValueError):
                weight = earned = 0
            requirements.append({**r, "weight": weight, "earned": earned})
            total_earned += earned
        score = max(0, min(100, round(total_earned)))
        missing = []
        for g in obj.get("missing", []):
            if isinstance(g, dict):
                missing.append({"gap": str(g.get("gap", "")).strip(),
                                 "why": str(g.get("why", "")).strip(),
                                 "example": str(g.get("example", "")).strip()})
            else:   # tolerate a model that reverts to a flat string -- still show something real
                missing.append({"gap": str(g).strip(), "why": "", "example": ""})
        project_assessment = []
        for pa in obj.get("project_assessment", []):
            try:
                row = by_id.get(int(pa.get("item_id", -1)))
            except (TypeError, ValueError):
                continue   # non-numeric hallucinated id -- don't waste the whole call over one bad entry
            if row is None or row["entry_type"] != "project":
                continue
            project_assessment.append({"row_id": row["id"], "name": row["company"],
                                        "worth_including": bool(pa.get("worth_including")),
                                        "reason": str(pa.get("reason", "")).strip()})
    except Exception as e:
        print(f"[jobs] {provider} score-requirements parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
        return None
    return {
        "requirements": requirements,
        "missing": missing,
        "project_assessment": project_assessment,
        "score": score,
        "reason": reason,
    }



def select_content(job, profile, point_bank_items, skill_names, candidate_years, requirements,
                   forced_project_ids=None, advisory_project_ids=None):
    """Call 2 of the resume judge/selector split: picks which role items go
    on THIS resume, truthfully rewords them, and picks skills -- grounded
    in Call 1's (score_requirements) already-computed `requirements`
    (read-only context here, not re-derived) so the two calls agree on what
    matters without redoing each other's work. Generation-only -- never
    called from _score_only, since score-only callers threw this exact work
    away unused every time.

    Role items get their own worth_including judgment here. Projects don't
    get re-judged here -- inclusion for a project is decided before this
    call, by one of two things: forced_project_ids (the user explicitly
    chose it via "Add project to resume") or advisory_project_ids (Call 1
    already judged it genuinely worth_including for this JD). Once a
    project is in either set, this call's only job for it is the same
    truthful reword/JD-vocabulary treatment role items get -- never a
    second worth-including judgment Call 1 already made. The two sets stay
    separate for the CALLER (only forced_project_ids is caller-protected
    from the one-page trim -- an advisory inclusion should be just as
    trimmable as any other normal item), but are treated identically here.
    forced_project_ids: project item ids the user explicitly chose to
    include via the "Add project to resume" button -- these DO land in
    priority_items regardless of Call 1's relevance judgment, appended
    after the role items; everything else stays bullets-only.

    Carries the SAME writing-quality disciplines the old combined call
    used -- HUMAN_STYLE_RULES baked into the prompt, AND a real code-level
    humanizer.check_human_style() gate on every included bullet's text,
    with up to 2 retries feeding violations back.

    `kind` (role vs project) is deliberately NOT asked of the model -- it's
    filled in afterward from the real DB row's entry_type, never trusted
    from the reply. priority_items is also defensively filtered: a project
    id only ever lands in it if it's in forced_project_ids, even if the
    model includes one anyway despite instructions -- the DB/caller decide
    what's allowed in, not the model's compliance with a prompt rule.

    Returns {"priority_items", "skills", "role_assessment"}, or None if the
    call/parse/style-gate failed."""
    role_items_text = "\n".join(
        f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
        for p in point_bank_items if p["entry_type"] == "role" and not p["text"].lstrip().startswith("["))
    requirements_text = "\n".join(
        f"- {r.get('requirement', '')} (importance={r.get('importance', '')}, weight={r.get('weight', 0)}, "
        f"evidence_item_id={r.get('evidence_item_id')})"
        for r in requirements) or "(none identified)"
    system = (
        "You build the tailored resume content for one real resume, against one real job posting -- "
        "truthful selection and wording only, never inventing. You're given the candidate's role items "
        "(real work-experience accomplishments), and this posting's already-scored requirements "
        "(computed separately -- treat them as given, don't re-derive or second-guess them). Do two "
        "things:\n\n"
        "1. CONTENT SELECTION -- bullets only, from the role items. For EVERY role item, give an "
        "explicit verdict: worth_including (true/false) and a one-sentence reason, even for items "
        "you're excluding -- this is the audit trail for why each item did or didn't make the cut, not "
        "just the final list. Two reasons an item earns a place: it's the evidence for a real "
        "requirement above, or it's simply a strong, impressive accomplishment worth showing even "
        "without a direct JD match (a one-page resume with room to spare should never go half-empty "
        "rather than show real strength). Order every item you're including by real value for THIS "
        "resume, strongest first: requirement-evidence items (by how well they satisfy a required vs. "
        "nice-to-have requirement) before pure-backfill items. Don't hold back for page-length reasons "
        "-- include everything genuinely worth showing; a separate, real page-count check downstream "
        "trims the list if it runs long, worst-value items first, by the order you give.\n"
        "   For EVERY item you're including, run TWO separate, independent checks before ever "
        "considering whether to omit the text field -- these are two different reasons to reword, not "
        "one, and a pass on either one alone is NOT enough to skip rewording:\n"
        "   CHECK A (JD vocabulary): does the JD name a specific tool, technique, or concept that this "
        "item genuinely, truthfully demonstrates but describes in different words? If yes, reword to "
        "use the JD's term. This also covers the JD's own REPEATED framing/theme words, not just named "
        "tools -- if the JD's title or opening paragraph centers on a word like \"distributed systems\", "
        "\"real-time\", or \"high-throughput\" and an item's real, true work genuinely fits that framing "
        "(e.g. a large multi-pipeline data platform really is a distributed system, even if the point-"
        "bank text never used that exact word), use the JD's own word for it. Checked live: a real "
        "generate for a posting titled \"Distributed Systems\" that used the word four times never used "
        "it once anywhere in the resume or cover letter, despite the underlying work genuinely "
        "qualifying -- that's a missed match, not appropriate restraint.\n"
        "   CHECK B (plain language -- the one that gets skipped if you're not deliberate about it): "
        "read the item's ORIGINAL point-bank wording as if you'd never seen the system before. Would "
        "someone outside this exact specialty follow what was done and why it mattered on one read? "
        "Point-bank text is written for the candidate's own records, not for a stranger skimming for a "
        "few seconds -- it is allowed to be denser or more jargon-heavy than a resume should be, and "
        "the fact that it's already on file is NOT evidence it's already plain enough. If it leans on "
        "unexplained acronyms, stacked technical nouns, or a sentence that needs a second read, reword "
        "it in plainer words even though the facts and the JD-match don't change. This check fires on "
        "most items in a real resume, not a rare few -- if you are about to leave most of the role "
        "items unchanged, go back and actually re-read each original sentence as a stranger would "
        "before concluding it was already plain.\n"
        "   The text field may ONLY be omitted when BOTH checks come back negative -- no JD-vocabulary "
        "swap needed AND the original wording is already plain enough for a non-specialist to follow in "
        "one read. If either check is positive, you must write out the real reworded text, never retype "
        "the original unchanged and never omit it. Never add a fact, number, or outcome that isn't "
        "already there -- only reword what's genuinely, truthfully present. Every bullet, reworded or "
        "not, must carry a real named skill/technology and how it was used, and/or a measurable result "
        "(a number, a named outcome, an award) -- never a bare responsibility with nothing backing it "
        "(don't open on \"Implemented X\" or \"Worked on Y\" with nothing else). The goal is a reader "
        "immediately grasping the impact in a few seconds, not a display of domain vocabulary -- if a "
        "technical term is genuinely the right word, the surrounding words still have to carry the real "
        "accomplishment on their own. And any wording you do write must follow these rules exactly:\n"
        + HUMAN_STYLE_RULES + "\n"
        "   Separately, if the user message below lists any Project items to include, each one is "
        "already decided -- either the candidate explicitly chose it, or a separate earlier judgment "
        "already found it genuinely worth including on this resume. For each, decide only whether to "
        "reword it (same truthful-reword/JD-vocabulary rule and style rules as role items above) and "
        "report it under forced_project_items; you are not judging whether to include it, only how to "
        "word it. The plain-language rule above applies especially hard here -- project "
        "bullets tend to carry the densest jargon (model/framework names, ML technique acronyms), and "
        "a reader who doesn't already know that specific stack should still come away understanding "
        "what was actually built and why it was hard, not just see a wall of technology names.\n\n"
        "2. SKILLS -- from the candidate's tagged skills given below, pick the ones genuinely relevant "
        "to this JD. Only include a \"display\" value when the JD's own exact term is a truthful match "
        "for something the candidate actually tagged and reads better than the original (e.g. "
        "candidate has \"REST APIs\" tagged, JD says \"APIs\" -- display \"APIs\"); leave it out "
        "entirely when the original tag text is already fine, the caller uses the original name by "
        "default. Never invent a skill the candidate didn't tag. Order by relevance to this JD, most "
        "relevant first.\n\n"
        "Reply with ONLY a JSON object: {\"role_assessment\": [{\"item_id\": int, \"worth_including\": "
        "bool, \"reason\": str, \"text\": str (OMIT this field entirely, don't send an empty string, "
        "when you're not changing the item's wording)}], \"forced_project_items\": [{\"item_id\": int, "
        "\"text\": str (OMIT when not changing wording)}] (empty list if none were listed), "
        "\"skills\": [{\"name\": str, \"display\": str (OMIT when the original tag name is already "
        "fine)}]}."
    )
    forced = set(forced_project_ids or [])
    include_ids = forced | set(advisory_project_ids or [])
    forced_note = ""
    if include_ids:
        forced_rows = [p for p in point_bank_items if p["entry_type"] == "project" and p["id"] in include_ids]
        forced_text = "\n".join(
            f"[id={p['id']} | {p['company']} -- {p['role']}, {p['start_date']} to {p['end_date'] or 'present'}] {p['text']}"
            for p in forced_rows)
        forced_note = f"\n\nProject items to include (id | project -- tagline, dates):\n{forced_text}"
    user = (
        f"Candidate: {profile.get('title', '')}, {candidate_years} years of real experience.\n\n"
        f"Role items (id | company -- role, dates):\n{role_items_text}\n\n"
        f"Already-scored requirements for this posting (treat as given):\n{requirements_text}\n\n"
        f"Known skills (tags only -- no depth signal by themselves): {', '.join(skill_names)}\n\n"
        f"Job posting -- {job['title']} at {job['company']}:\n{trim_text(job['description_raw'])}"
        + forced_note
    )
    messages = [{"role": "user", "content": user}]
    known_lower = {s.lower() for s in skill_names}
    by_id = {p["id"]: p for p in point_bank_items}
    for attempt in range(3):
        reply, provider = llm.llm_json_call(system, messages, "select", max_tokens=32000, temperature=0.2)
        if reply is None:
            return None
        try:
            m = re.search(r"\{.*\}", reply, re.S)   # tolerate stray text/fences around the JSON
            obj = json.loads(m.group(0) if m else reply)
            priority_items = []
            role_assessment = []
            for it in obj.get("role_assessment", []):
                try:
                    row = by_id.get(int(it["item_id"]))
                except (TypeError, ValueError):
                    # Non-numeric id (seen live: "93_alt_unused") -- the
                    # model inventing a variant id, never a real row.
                    # Skip it the same as any other hallucinated id,
                    # don't let one bad entry crash the whole parse.
                    continue
                if row is None or row["entry_type"] != "role":   # hallucinated/stale id, or not a role item -- skip
                    continue
                worth = bool(it.get("worth_including"))
                role_assessment.append({"row_id": row["id"], "worth_including": worth,
                                         "reason": str(it.get("reason", "")).strip()})
                if worth:
                    text = str(it.get("text") or "").strip() or row["text"]
                    priority_items.append({"row_id": row["id"], "text": text, "kind": row["entry_type"]})
            for it in obj.get("forced_project_items", []):
                try:
                    row = by_id.get(int(it["item_id"]))
                except (TypeError, ValueError):
                    continue   # non-numeric hallucinated id -- same tolerance as role_assessment above
                if row is None or row["entry_type"] != "project" or row["id"] not in include_ids:
                    continue   # projects only land here if forced or Call-1-approved -- enforced here, not trusted from the prompt
                text = str(it.get("text") or "").strip() or row["text"]
                priority_items.append({"row_id": row["id"], "text": text, "kind": row["entry_type"]})
            if not priority_items:
                print(f"[jobs] {provider} select-content returned zero usable priority_items for "
                      f"job {job['id']}", file=sys.stderr)
                return None
            skills = []
            for s in obj.get("skills", []):
                name = str(s.get("name", "")).strip()
                if name.lower() not in known_lower:   # never show a skill the candidate didn't actually tag
                    continue
                skills.append({"name": name, "display": str(s.get("display", name) or name).strip()})
        except Exception as e:
            print(f"[jobs] {provider} select-content parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
            return None
        violations = []
        for it in priority_items:
            for v in humanizer.check_human_style(it["text"]):
                violations.append(f'bullet "{it["text"][:60]}...": {v}')
        if not violations:
            return {
                "priority_items": priority_items,
                "skills": skills,
                "role_assessment": role_assessment,
            }
        if attempt < 2:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": (
                "Some bullets fail the style check -- fix these and reply with the same JSON shape, "
                "nothing else:\n- " + "\n- ".join(violations))})
    # Last attempt still has violations -- one stubborn bullet used to fail the ENTIRE call,
    # throwing away 9 good reworded items because of 1 the model couldn't fix in 3 tries.
    # Found live on a real generate. Per-item salvage instead: revert ONLY the still-
    # violating items to their real, untouched point-bank text (which is independently
    # checked -- if the ORIGINAL also fails, that one item is dropped, never shipped
    # unchecked) and keep every other item's successfully reworded text. A slightly-
    # unpolished real bullet beats failing the whole resume.
    salvaged, dropped = [], []
    for it in priority_items:
        if humanizer.check_human_style(it["text"]):
            row = by_id.get(it["row_id"])
            original = row["text"] if row is not None else ""
            if original and not humanizer.check_human_style(original):
                salvaged.append({**it, "text": original})
            else:
                dropped.append(it["row_id"])
        else:
            salvaged.append(it)
    if dropped:
        print(f"[jobs] {provider} select-content: dropped row_ids {dropped} for job {job['id']} -- "
              "neither the reworded nor the original text passed the style check", file=sys.stderr)
    if not salvaged:
        print(f"[jobs] {provider} select-content failed the humanizer gate 3x for job {job['id']}, "
              "nothing salvageable", file=sys.stderr)
        return None
    return {"priority_items": salvaged, "skills": skills, "role_assessment": role_assessment}


def build_selection(job, profile, point_bank_items, skill_names, candidate_years, scored,
                    forced_project_ids=None):
    """Call 2 plus the project-inclusion rules, given Call 1's `scored` (score_requirements).
    Returns the selection dict generate.render_application takes, or None if the
    call/parse/style-gate failed (the caller records that as a failed step, never as
    an empty resume)."""
    # A project Call 1 already judged genuinely worth_including for this JD now actually
    # lands on the resume instead of sitting advisory-only until the user manually clicks
    # "Add to resume" -- found live: a real generate for a distributed-systems posting
    # produced zero Projects section even though Kapitsa was real, relevant, true work.
    # Kept separate from forced_project_ids below (not trim-protected) since this is a
    # relevance call, not something the user explicitly insisted on.
    #
    # Explicitly Kapitsa only -- told directly: exactly one project on the resume, and it's
    # Kapitsa, never Curie (a tool that writes resumes showing up as a bullet ON a resume
    # reads oddly, and Kapitsa is the stronger, more complete story). Filtered here, not
    # left to Call 1's own relevance judgment, since Call 1 has in the past judged Curie
    # worth_including for some postings -- that's no longer a judgment call this makes.
    advisory_project_ids = {pa["row_id"] for pa in scored["project_assessment"]
                             if pa["worth_including"] and pa["name"] == "Kapitsa"}
    # Separately, always OFFER the current Kapitsa row as a low-priority fill candidate
    # too, even when Call 1 said it's not core-relevant -- real empty space at the bottom
    # of a page with zero worth_including projects just stayed empty, because a project
    # never got a chance to be considered unless Call 1 approved it outright. This is
    # deliberately NOT added to advisory_project_ids (so generate.py's trim-protection
    # stays limited to genuine approvals) -- a fallback filler should only survive when
    # there's real spare room, same real-rendered-page-count logic the one-page trim
    # already uses, never promoted to "must survive even under page pressure."
    kapitsa_rows = [p for p in point_bank_items
                    if p["entry_type"] == "project" and p["company"] == "Kapitsa"]
    fallback_project_ids = {max(kapitsa_rows, key=lambda r: r["id"])["id"]} if kapitsa_rows else set()
    # If the caller didn't explicitly force a project (the normal "just click Generate"
    # path -- no one clicked "Add project to resume"), but Call 1 genuinely judged Kapitsa
    # worth including for THIS JD, promote that into the forced set so it actually survives
    # the one-page trim. Found live, repeatedly: the default path kept producing resumes
    # with zero Projects section even when Call 1 had approved Kapitsa, because merely
    # "advisory" inclusion is NOT trim-protected -- a prior version of this code deliberately
    # left it that way, reasoning an approval shouldn't out-rank role bullets by default, but
    # the standing instruction here is "add Kapitsa whenever it's genuinely relevant," not
    # "only if the user remembers to force it in."
    effective_forced_ids = set(forced_project_ids or ()) or set(advisory_project_ids)
    selected = select_content(job, profile, point_bank_items, skill_names, candidate_years,
                              scored["requirements"], forced_project_ids=effective_forced_ids,
                              advisory_project_ids=advisory_project_ids | fallback_project_ids)
    if selected is None:
        return None
    priority_items = selected["priority_items"]
    # "Exactly one project on the resume" is the rule -- but Call 2 is offered
    # forced_project_ids AND advisory_project_ids as separate candidate lists, and
    # nothing stopped it from including more than one if it judged several relevant.
    # Found live: a real run included both the forced Kapitsa row and the fallback
    # Kapitsa row, inflating the Projects section to 2 bullets and squeezing role
    # bullets out of the one-page budget. Hard cap at one here, preferring whichever
    # row was actually forced (an explicit user insistence beats the model's own pick).
    project_items = [it for it in priority_items if it["kind"] == "project"]
    if len(project_items) > 1:
        keep = next((it for it in project_items if it["row_id"] in effective_forced_ids), project_items[0])
        priority_items = [it for it in priority_items if it["kind"] != "project"] + [keep]
    # Offering a candidate to Call 2 only works if the model actually bothers to emit it --
    # it's told "you are not judging whether to include it, only how to word it" for every
    # offered id, but that's a request, not a guarantee. Found live: a real run with real
    # spare page room (both companies well under the cap) still produced zero project
    # items, because the model's reply simply never mentioned one despite candidates being
    # listed. Deterministic backstop: if nothing with kind="project" made it through at
    # all, append the best available project's real, untouched point-bank text directly --
    # same spirit as _balance_role_bullets' backfill, a guarantee that doesn't depend on
    # the model choosing to cooperate.
    if not any(it["kind"] == "project" for it in priority_items):
        by_id = {p["id"]: p for p in point_bank_items}
        fallback_id = next(iter(advisory_project_ids), None) or next(iter(fallback_project_ids), None)
        row = by_id.get(fallback_id)
        # Still goes through the same style gate every other bullet does -- this bypasses
        # Call 2's own already-passed violations check (the raw text was never part of that
        # reply), so it gets its own check rather than trusting unchecked text onto the page.
        if row is not None and not humanizer.check_human_style(row["text"]):
            priority_items = priority_items + [{"row_id": row["id"], "text": row["text"], "kind": "project"}]
    return {
        "priority_items": priority_items,
        "requirements": scored["requirements"],
        "project_assessment": scored["project_assessment"],
        "skills": selected["skills"],
        "missing": scored["missing"],
        "score": scored["score"],
        "reason": scored["reason"],
        # Carried through so generate.py's page-trim never silently drops a project the
        # user explicitly forced on via "Add to resume" -- OR one Call 1 itself judged
        # genuinely worth including, now promoted into this same set above.
        "forced_project_ids": list(effective_forced_ids),
        # Genuine Call-1 approvals only (never the fallback-filler ids) --
        # generate.py uses this to decide which project, if any, earns the
        # "survive the one-page trim" floor. A fallback filler never does;
        # it only ever shows up when there's real spare room.
        "advisory_project_ids": list(advisory_project_ids),
    }


def _points_text_for_selection(point_bank_items, priority_items):
    """Scoped points_text for draft_application -- only the items
    actually selected for the resume (priority_items), using each item's
    FINAL text (reworded by Call 2, or original if unchanged). Fixes the
    cover letter being able to reference a fact the resume doesn't include:
    it used to be drafted from the entire point bank, before content
    selection had even run."""
    by_id = {p["id"]: p for p in point_bank_items}
    lines = []
    for it in priority_items:
        row = by_id.get(it["row_id"])
        if row is None:
            continue
        lines.append(f"[{row['company']} -- {row['role']}, {row['start_date']} to "
                      f"{row['end_date'] or 'present'}] {it['text']}")
    return "\n".join(lines)


def draft_application(job, profile, points_text, skill_names, candidate_years):
    """Writes a resume summary + cover letter body for one job, grounded
    strictly in the real point bank -- never invents a fact, number, or
    accomplishment. Returns (summary, cover_letter_body, None) on success,
    or (None, None, reason) on failure -- reason distinguishes "the Zai
    call itself failed" (no key, rate-limited, network) from "it replied
    but never passed the humanizer gate in 3 tries", since those need
    different responses (wait and retry vs. look at what it kept writing).

    Resolves the tension the original hand-written-only design existed to
    avoid: a blind LLM call producing generic "I am excited to apply..."
    AI-slop. The fix isn't "no LLM", it's a real enforced gate -- every
    draft is checked with humanizer.check_human_style() (the same hard
    gate generate() itself refuses to render past) before it's accepted,
    with up to 2 retries feeding the exact violations back if it fails."""
    system = (
        "You write a tailored resume summary and cover letter for one real job posting, "
        "grounded ONLY in the candidate's real accomplishments given below. Never invent a "
        "fact, number, project, or outcome not literally present in the point bank text -- "
        "if the posting wants something the point bank doesn't cover, don't claim it, just "
        "don't mention it. Be specific: reference real numbers and real systems from the "
        "point bank, and real specifics from the posting (the team, the problem, a named "
        "technology) -- not generic enthusiasm. If the posting's own title or opening "
        "paragraph repeats a theme word (e.g. \"distributed systems\", \"real-time\", "
        "\"high-throughput\") and the candidate's real work genuinely fits that framing, use "
        "the posting's own word for it -- don't describe the same true work in different "
        "language just because the point bank happened to phrase it differently.\n\n"
        + HUMAN_STYLE_RULES +
        "\nResume summary: exactly 2 sentences, no greeting, same shape every time so it reads "
        "as one consistent, deliberate statement rather than a different structure each run:\n"
        "   Sentence 1: years of real experience + the single most impressive, concrete scale "
        "fact from the point bank (a real number -- data volume, request rate, pipeline count, "
        "whatever is strongest), tied to whichever company it actually happened at.\n"
        "   Sentence 2: one more concrete differentiator from a DIFFERENT accomplishment or "
        "company than sentence 1 used, picked for relevance to THIS posting -- never repeat "
        "the same fact sentence 1 already used.\n"
        "   TENSE, checked against the real dates on each point-bank item (every item is "
        "tagged with its own start/end date): if the role's end date is a real date in the "
        "past, every accomplishment from it is PAST tense ONLY -- \"built\", \"owned\", \"cut\", "
        "\"drove\" -- never \"I build\", \"I own\", \"I run\", which claims the candidate is "
        "currently doing it. Only a role with no end date (still ongoing) may use present "
        "tense. Check every single role used in the summary before writing -- if ALL the roles "
        "referenced have already ended, the ENTIRE summary is past tense, with no exceptions "
        "and no mixing -- writing \"I build X\" about a job that ended months ago misstates the "
        "candidate's current employment status, which is a real factual error, not a style "
        "choice. Pick ONE clear, concrete way to describe what the candidate actually did in "
        "each accomplishment -- never blend two different framings in the same sentence (e.g. "
        "\"owned the pipelines\" and \"built the platform other teams used\" are two different "
        "claims; pick whichever is literally true and say only that, plainly, so a reader isn't "
        "left guessing which one it actually was). Tone is warm and a little humble -- never "
        "phrase anything as the candidate being able to do what others can't, or any other "
        "comparison that puts them above unnamed others; let the real facts make the case "
        "instead of a claim of superiority.\n"
        "Cover letter body: 3-4 short paragraphs, no \"Dear Hiring Manager\" greeting or "
        "sign-off (the renderer adds those), grounded in specific real point-bank facts "
        "connected to specific things this posting actually asks for.\n\n"
        "Reply with ONLY a JSON object: {\"summary\": str, \"cover_letter_body\": str} "
        "(cover_letter_body paragraphs separated by a blank line, i.e. \\n\\n)."
    )
    user = (
        f"Candidate: {profile.get('title', '')}, {candidate_years} years of real experience.\n\n"
        f"Real accomplishments, with company/role/dates:\n{points_text}\n\n"
        f"Known skills: {', '.join(skill_names)}\n\n"
        f"Job posting -- {job['title']} at {job['company']}:\n{trim_text(job['description_raw'])}"
    )
    messages = [{"role": "user", "content": user}]
    # 4, was 3 -- this call has no salvage fallback (unlike bullet selection, which can
    # revert to the real point-bank text when a reword keeps failing). A style-gate loss
    # here kills the whole generation outright, so it gets one extra attempt with the
    # explicit violations fed back before giving up.
    draft_attempts = 4
    for attempt in range(draft_attempts):
        reply, provider = llm.llm_json_call(system, messages, "draft", max_tokens=32000, temperature=0.4)
        if reply is None:
            return None, None, "LLM call failed on both Zai and Claude -- see server logs (often a rate limit; check for a 429)"
        try:
            m = re.search(r"\{.*\}", reply, re.S)
            obj = json.loads(m.group(0) if m else reply)
            summary, cover = str(obj["summary"]).strip(), str(obj["cover_letter_body"]).strip()
        except Exception as e:
            print(f"[jobs] {provider} draft parse failed: {e} -- raw: {reply[:200]}", file=sys.stderr)
            return None, None, f"{provider} reply wasn't valid JSON: {e}"
        violations = (humanizer.check_human_style(summary)
                      + humanizer.check_human_style(cover))
        # An empty/near-empty string trivially passes every style check
        # above (there's nothing in it to flag) -- found live: a real
        # resume went out with a blank Summary section because this gate
        # never actually required non-empty content, just "no bad
        # patterns in whatever's there, including nothing."
        if len(summary) < 20:
            violations.append("summary is empty or too short -- write the real 1-2 sentence summary, not nothing")
        if len(cover) < 50:
            violations.append("cover_letter_body is empty or too short -- write the real 3-4 paragraph cover letter, not nothing")
        if not violations:
            return summary, cover, None
        if attempt < draft_attempts - 1:
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": (
                "That draft fails the style check -- fix these and reply with the same "
                "JSON shape, nothing else:\n- " + "\n- ".join(violations))})
    reason = f"failed the humanizer gate {draft_attempts}x in a row: " + "; ".join(violations)
    print(f"[jobs] {provider} draft for job {job['id']} {reason}", file=sys.stderr)
    return None, None, reason


# ── the run ──────────────────────────────────────────────────────────────
class StepError(Exception):
    """A step failed. kind is an analysis.error_kind; permanent means
    retrying can't help (bad input), so the row goes straight to failed."""

    def __init__(self, kind, msg, permanent=False):
        super().__init__(msg)
        self.kind, self.msg, self.permanent = kind, msg, permanent


def slugify(text, limit=60):
    """A safe, bounded path component: company names are free text."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-") or "x"


def master_doc_problem(profile, point_bank):
    """Why this master document can't produce a resume, or None if it can."""
    missing = [k for k in REQUIRED_PROFILE if not profile.get(k)]
    if missing:
        return "profile is missing: " + ", ".join(missing)
    if not any(p["entry_type"] == "role" for p in point_bank):
        return "the master document has no work-experience entries"
    return None


def master_doc_ready(user_id):
    return master_doc_problem(db.profile_all(user_id), db.point_bank_all(user_id)) is None


def job_dir(user_id, company, job_id):
    return os.path.join(FILES_DIR, str(user_id), slugify(company), str(job_id))


def remove_files(rel_paths):
    """Delete the folders the given data/-relative file paths live in. Only
    ever inside FILES_DIR, whatever the stored path says."""
    for rel in rel_paths:
        folder = os.path.dirname(os.path.normpath(os.path.join(db.DATA_DIR, rel)))
        if folder.startswith(FILES_DIR + os.sep):
            shutil.rmtree(folder, ignore_errors=True)


def _step(user_id, job_id, attempt, name, fn):
    """Run one pipeline step and record its metrics. fn() returns its result,
    or None when an LLM call failed -- turned into the right StepError here."""
    started, error = time.time(), None
    llm.take_usage()
    try:
        result = fn()
        if result is None:
            timed_out = llm.budget_exceeded()
            raise StepError("timeout" if timed_out else "llm",
                            "timeout" if timed_out else f"{name}: LLM call failed or returned unusable content")
        return result
    except StepError as e:
        error = e.msg
        raise
    except BaseException as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        db.analysis_step_add(user_id, job_id, attempt, name, started, int((time.time() - started) * 1000),
                             error is None, provider=llm.last_provider() if error is None else None,
                             usage=llm.take_usage(), error=error)


def _clean_requirements(requirements):
    """Coerce the model's free-text enums into the values the table allows
    (a bad value should cost a default, not the whole run)."""
    out = []
    for r in requirements:
        text = str(r.get("requirement", "")).strip()
        if not text:
            continue
        weight = min(100, max(0, int(r.get("weight", 0) or 0)))
        try:
            evidence = int(r["evidence_item_id"])
        except (KeyError, TypeError, ValueError):
            evidence = None
        out.append({
            "requirement": text,
            "importance": r.get("importance") if r.get("importance") in ("required", "nice_to_have") else "required",
            "presence": r.get("presence") if r.get("presence") in ("yes", "semantic", "no") else "no",
            "depth": r.get("depth") if r.get("depth") in ("quantified", "named", "n/a") else "n/a",
            "evidence_point_id": evidence, "weight": weight,
            "earned": min(weight, max(0, int(r.get("earned", 0) or 0))),
        })
    return out


def _render(job, profile, skills, point_bank, summary, cover, selection, out_dir):
    """generate_cli.py in the project venv, killed (whole process group, so a
    stuck soffice dies too) when the budget runs out. Returns its parsed result."""
    left = llm.remaining()
    if left is None or left < MIN_RENDER_SECONDS:
        raise StepError("timeout", "timeout")
    os.makedirs(out_dir, exist_ok=True)
    req_path = os.path.join(out_dir, "request.json")
    with open(req_path, "w") as f:
        json.dump({"job": {k: job[k] for k in ("title", "company", "url", "description_raw")}, "profile": profile,
                   "skills": skills, "point_bank": point_bank, "summary": summary, "cover_letter_body": cover,
                   "selection": selection, "out_dir": out_dir}, f)
    proc = subprocess.Popen([VENV_PY if os.path.exists(VENV_PY) else sys.executable,
                             os.path.join(ROOT, "generate_cli.py"), req_path],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
                            start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=left)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise StepError("timeout", "timeout")
    finally:
        if os.path.exists(req_path):
            os.unlink(req_path)
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise StepError("render", f"render produced no result: {(stderr or stdout)[-300:]}")
    if "error" in result:
        raise StepError("render", result["error"][:500])
    return result


def _draft(job, profile, point_bank, skill_names, years, selection):
    points_text = _points_text_for_selection(point_bank, selection["priority_items"])
    summary, cover, err = draft_application(job, profile, points_text, skill_names, years)
    if summary is None:
        if llm.budget_exceeded():
            raise StepError("timeout", "timeout")
        raise StepError("llm", f"draft: {err}")
    return summary, cover


def _run(user_id, job_id, attempt, tmp_dir):
    job = db.jobs_get(job_id)
    if job is None:
        raise StepError("invalid", "job no longer exists", permanent=True)
    profile = db.profile_all(user_id)
    point_bank = db.point_bank_all(user_id)
    problem = master_doc_problem(profile, point_bank)
    if problem:
        raise StepError("invalid", problem, permanent=True)
    skills = db.skills_all(user_id)
    skill_names = [s["name"] for s in skills]
    try:
        years = float(profile.get("years_experience") or 0)
    except ValueError:
        years = 0.0

    scored = _step(user_id, job_id, attempt, "score",
                   lambda: score_requirements(job, profile, point_bank, years))
    selection = _step(user_id, job_id, attempt, "select",
                      lambda: build_selection(job, profile, point_bank, skill_names, years, scored))
    summary, cover = _step(user_id, job_id, attempt, "draft",
                           lambda: _draft(job, profile, point_bank, skill_names, years, selection))
    provider = llm.last_provider()
    rendered = _step(user_id, job_id, attempt, "render",
                     lambda: _render(job, profile, skills, point_bank, summary, cover, selection, tmp_dir))

    # Swap the finished folder into place, then record everything in one transaction.
    final = job_dir(user_id, job["company"], job_id)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    old = final + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(final):
        os.replace(final, old)
    os.replace(tmp_dir, final)
    shutil.rmtree(old, ignore_errors=True)

    def rel(name):
        return os.path.relpath(os.path.join(final, name), db.DATA_DIR)
    db.analysis_complete(
        user_id, job_id, scored["score"], scored["reason"], provider, rel("resume.pdf"), rel("cover_letter.pdf"),
        requirements=_clean_requirements(scored["requirements"]),
        gaps=[g for g in scored["missing"] if g["gap"]],
        projects=[{"point_id": p["row_id"], "worth_including": p["worth_including"], "reason": p["reason"]}
                  for p in scored["project_assessment"]],
        items=rendered["items"])


def process_one(user_id, job_id):
    """Run one claimed analysis row to completion. Returns the status it
    ended in: 'done', 'retry' or 'failed'."""
    attempt = db.analysis_attempt(user_id, job_id)
    db.analysis_start(user_id, job_id)
    llm.set_budget(BUDGET_SECONDS)
    os.makedirs(FILES_DIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix=f"tmp-{user_id}-{job_id}-", dir=FILES_DIR)
    try:
        _run(user_id, job_id, attempt, tmp_dir)
        return "done"
    except StepError as e:
        return db.analysis_fail(user_id, job_id, e.kind, e.msg, permanent=e.permanent)
    except Exception as e:
        traceback.print_exc()
        return db.analysis_fail(user_id, job_id, "internal", f"{type(e).__name__}: {e}")
    finally:
        llm.clear_budget()
        shutil.rmtree(tmp_dir, ignore_errors=True)   # already gone after a successful swap
