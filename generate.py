"""Tailor a resume to one job: render + one-page-fit whatever content
selection the caller already decided on, write the application artifacts.

Content selection and scoring both happen upstream now, in platform/
server.py's single LLM call (the only place in this whole pipeline with
LLM access -- this module stays plain and LLM-free, same as it's always
been, so it can run standalone in jobsearch/.venv with no API key). What
used to live here -- a greedy regex-score-maximizing bullet search
(`select_bullets_max_ats`, kept below but no longer called, see its own
docstring) -- is superseded by letting the LLM pick and word the content
directly, truthfully, against the real JD. `generate()` now takes that
decision (`selection`, see its docstring) as plain data, exactly the same
way it already took an LLM-authored `summary`/`cover_letter_body` as plain
strings -- never computes a selection or a score itself, only renders one,
measures the real page count, and trims by the given priority order if it
overflows (a physical fact about the rendered DOCX no prompt can see,
still handled deterministically here).

The summary line and cover letter body are NOT auto-templated here -- those
are written per job, grounded in the point bank and the posting's real
specifics, under HUMAN_STYLE_RULES (see platform/server.py in the C++ repo
for the original ruleset this reuses) -- and humanizer.check_human_style()
is a real, code-level gate on both before anything renders, not just a
comment telling whoever writes the text to follow the rules. A generic
script writing "I am excited to apply..." is exactly the AI-slop shape this
project exists to avoid; a person (or me, deliberately, per job) writing
from real facts is not something a template should try to shortcut.
"""

import difflib
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid

import ats
import db
import humanizer
import resume_render

# Same threshold and same difflib.SequenceMatcher math as
# db.point_bank_find_similar (already proven: catches real near-duplicate
# bullets, like one point edited into a second slightly-different version
# with the old one left active by mistake -- found exactly that pair in the
# real point bank while designing this). Character/sequence similarity, not
# semantic -- cheap, no API cost, and matches the actual failure pattern
# seen so far (an edited duplicate), not a hypothetical "differently-worded
# same fact" case there's no evidence of yet.
DUPLICATE_THRESHOLD = 0.82

# The upstream LLM selection (platform/server.py's _zai_select_content)
# optimizes purely for JD relevance, with no awareness of how the result
# looks split across companies. Found live on a real Conviva posting: 10
# LinkedIn bullets, 2 Cashfree -- correct by relevance, but reads as if the
# Cashfree years barely happened. Explicit, exact rule (not just a loose
# band): role bullets always total TARGET_TOTAL_ROLE_BULLETS, split between
# companies as 4-6, 5-5, or 6-4 -- never more lopsided, never a different
# total. A looser "somewhere between 4 and 6 each" version of this still
# let total bullet count drift (8, 9, 11...) between runs.
TARGET_TOTAL_ROLE_BULLETS = 10
MAX_BULLETS_PER_COMPANY = 6
MIN_BULLETS_PER_COMPANY = 4


def _balance_role_bullets(priority_items, point_bank_items):
    """Exact algorithm, not a loose cap/floor: role_items already comes in
    JD-relevance order, strongest first (Call 2's own contract -- "order
    every item you're including by real value for THIS resume"). Look at
    the top 10 of that ranked list to find the natural company split. 5-5
    there -- done, use those exact 10. Otherwise whichever company has more
    in the top 10 gets MAX_BULLETS_PER_COMPANY (6), the other gets
    MIN_BULLETS_PER_COMPANY (4) -- and critically, the short company's
    remaining slots are filled from FURTHER DOWN THE SAME RANKED LIST
    first (rank 11, 12, ... -- still real JD-relevance signal, just not in
    the top 10), never from an unranked id-order guess. Only once the
    ranked list itself runs out for that company does it fall back to real,
    not-yet-selected active point-bank rows in id order -- last resort,
    never fabricated, just no longer relevance-ordered because there's no
    relevance signal left to use. Role items only; projects are a separate
    section with their own inclusion rule upstream.

    Returns (balanced_priority_items, floor_protected_ids) -- the second is
    only each company's true MIN_BULLETS_PER_COMPANY floor, which the
    caller MUST pass into the one-page trim's own protected_ids. Protecting
    the FULL 10 was tried and found live to break the project guarantee --
    once every role bullet AND the one guaranteed project line were all
    "protected", a real overflow left nothing in the genuinely-unprotected
    pool, so the trim's own last-resort fallback (which ignores protection
    entirely once nothing else is left) fired immediately and always
    picked the project, since it sits last in priority_items -- the
    opposite of guaranteed. Protecting only the true floor and leaving the
    5th/6th-tier bullets and the project in the normal trimmable pool lets
    simple list order do the right thing instead: the project is always
    last, so a normal (non-last-resort) trim pass reaches it first and
    drops it before ever touching a role bullet -- which only works
    correctly now that the batch-overshoot bug (see generate()'s own trim
    loop) no longer blows through several items in one un-rerendered pass."""
    role_items = [it for it in priority_items if it["kind"] == "role"]
    other_items = [it for it in priority_items if it["kind"] != "role"]
    by_id = {r["id"]: r for r in point_bank_items}

    ranked = []   # [(company, item), ...] in Call 2's own given relevance order
    for it in role_items:
        row = by_id.get(it["row_id"])
        if row is not None:
            ranked.append((row["company"], it))

    company_order = []
    for company, _ in ranked:
        if company not in company_order:
            company_order.append(company)

    top10_counts = {}
    for company, _ in ranked[:10]:
        top10_counts[company] = top10_counts.get(company, 0) + 1

    if len(company_order) == 0:
        target = {}
    elif len(company_order) == 1:
        target = {company_order[0]: TARGET_TOTAL_ROLE_BULLETS}
    elif top10_counts.get(company_order[0], 0) == top10_counts.get(company_order[1], 0) == 5:
        target = {company_order[0]: 5, company_order[1]: 5}
    else:
        leader = max(company_order[:2], key=lambda c: top10_counts.get(c, 0))
        other = company_order[1] if leader == company_order[0] else company_order[0]
        target = {leader: MAX_BULLETS_PER_COMPANY, other: MIN_BULLETS_PER_COMPANY}
        for extra_company in company_order[2:]:
            target[extra_company] = 0   # more than 2 companies isn't the real case today -- don't guess, exclude

    by_company = {c: [] for c in company_order}
    for company, it in ranked:
        if len(by_company[company]) < target.get(company, 0):
            by_company[company].append(it)

    selected_ids = {it["row_id"] for items in by_company.values() for it in items}

    def available_for(company):
        return sorted(
            (r for r in point_bank_items
             if r["entry_type"] == "role" and r["company"] == company
             and r["id"] not in selected_ids),
            key=lambda r: r["id"])

    for company in company_order:
        short = target.get(company, 0) - len(by_company[company])
        if short <= 0:
            continue
        for row in available_for(company)[:short]:
            by_company[company].append({"row_id": row["id"], "text": row["text"], "kind": "role"})
            selected_ids.add(row["id"])

    floor_protected_ids = {
        it["row_id"] for items in by_company.values() for it in items[:MIN_BULLETS_PER_COMPANY]}
    balanced_role_items = [it for company in company_order for it in by_company[company]]
    return balanced_role_items + other_items, floor_protected_ids


def _cluster_duplicates(rows, threshold=DUPLICATE_THRESHOLD):
    """Groups near-duplicate point-bank rows so selection only ever lets
    ONE member of a cluster onto a given resume -- two phrasings of the
    same real accomplishment can't both occupy bullet slots, and can't
    double-count toward keyword coverage (which is exactly the risk: if
    both happen to contain the same keyword, the scorer would credit it
    twice for one real piece of evidence). Returns {row_id: cluster_id}."""
    clusters = []  # list of representative-normalized-text per cluster
    assigned = {}
    for r in rows:
        norm = re.sub(r"\s+", " ", r["text"].strip().lower())
        placed = False
        for ci, rep_norm in enumerate(clusters):
            if difflib.SequenceMatcher(None, norm, rep_norm).ratio() >= threshold:
                assigned[r["id"]] = ci
                placed = True
                break
        if not placed:
            clusters.append(norm)
            assigned[r["id"]] = len(clusters) - 1
    return assigned

MAX_PAGES = 1
# Hard ceiling on the greedy search below (bullets considered per step) --
# the point bank is expected to grow well past the ~30 rows it has today
# ("100 points" was the real number mentioned), and re-scoring every
# remaining candidate every step is O(n^2) in bullet count. 60 is far more
# than a one-page resume could ever fit, so this never actually binds in
# practice -- it's a ceiling against a future point bank in the hundreds,
# not a meaningful cap today.
MAX_CANDIDATES = 60


def select_bullets_max_ats(job, resume_title, summary, skill_names, skills_line_text, candidate_years):
    """Superseded -- kept, not deleted, but no longer called from generate().
    Content selection now happens via one LLM call upstream (platform/
    server.py), which picks AND words bullets together, truthfully, against
    the real JD -- regex keyword-coverage maximization isn't a meaningful
    proxy for that anymore, and this greedy search plus the whole
    ats.score_resume()-driven selection loop below is dead weight on the
    new path. Left intact in case the regex-only approach is ever wanted
    again standalone.

    Ranks every real point-bank bullet by its marginal contribution to
    ats.score_resume() for THIS job, then includes ALL of them by default
    -- not just whichever small subset first maxes out the score.

    An earlier version stopped as soon as no remaining bullet raised the
    score further, which sounds right but isn't: score_resume() rewards
    matched-skill COVERAGE, so once every skill the JD asks for already has
    a strong bullet behind it, adding more real bullets (different
    accomplishments, same skills, or skills this JD doesn't ask for) can't
    move the number -- so the greedy search plateaued at 2-3 bullets out of
    15 real ones. That's "ATS-optimal" by the narrow metric and a resume
    no human reviewer would take seriously: it throws away most of a real
    body of work to chase a score that was already maxed. Checked directly
    (rendered the full 15-bullet set through the tight layout in
    resume_render.py and confirmed via pdfinfo): it fits one page anyway,
    so there was never a real reason to cut.

    So: keep going past the plateau, ranking what's left by the same
    marginal-value math (ties or small drops included) instead of
    stopping. Every real bullet ends up in the resume, ordered by real
    ATS contribution -- highest-value first in `selection_order`, which is
    exactly the order generate()'s one-page trim (a real fallback for when
    the point bank eventually grows past what one page can hold, not the
    normal case today) cuts from the back of.

    Returns (roles, selection_order, final_score). roles is already
    grouped/ordered the way resume_render expects, each role's own bullets
    kept in natural point-bank order (not selection order), so the resume
    still reads as a coherent role narrative, not a relevance-sorted
    jumble."""
    jd_text = job["description_raw"]
    rows = [r for r in db.point_bank_all() if not r["text"].lstrip().startswith("[")][:MAX_CANDIDATES]
    cluster_of = _cluster_duplicates(rows)

    selection_order = []   # in the order chosen -- first = highest marginal value
    remaining = list(rows)
    best_score = -1
    while remaining:
        best_row, best_trial_score = None, -1
        for r in remaining:
            trial_text = "\n".join(x["text"] for x in selection_order + [r])
            trial = ats.score_resume(resume_title, summary, trial_text, skills_line_text,
                                      jd_text, skill_names, candidate_years)
            if trial["score"] > best_trial_score:
                best_trial_score, best_row = trial["score"], r
        selection_order.append(best_row)
        best_score = max(best_score, best_trial_score)
        # Drop every near-duplicate of the row just picked, not just the
        # row itself -- a second phrasing of the same accomplishment has
        # no real marginal value left to offer once its cluster-mate is in.
        picked_cluster = cluster_of[best_row["id"]]
        remaining = [r for r in remaining if cluster_of[r["id"]] != picked_cluster]

    return _group_into_roles(selection_order), selection_order, best_score


def _group_into_roles(selected_rows):
    """selected_rows: point-bank rows in selection order (highest marginal
    value first). Re-groups by (company, role), each role's own bullets
    kept in natural point-bank order (not selection order) so a role still
    reads top-to-bottom the way it actually happened, and roles ordered
    most-recent-first the way point_bank_all() already sorts."""
    by_role = {}
    seen_keys = []
    for r in selected_rows:
        key = (r["company"], r["role"])
        if key not in seen_keys:
            seen_keys.append(key)
        by_role.setdefault(key, []).append(r)

    all_rows_by_id = {r["id"]: r for r in db.point_bank_all()}
    roles = []
    for key in seen_keys:
        items = sorted(by_role[key], key=lambda r: r["id"])
        company, role = key
        end = items[0]["end_date"] or "Present"
        roles.append({
            "company": company,
            "role": role,
            "dates": f"{items[0]['start_date']} - {end}",
            "bullets": [r["text"] for r in items],
            "_row_ids": [r["id"] for r in items],  # internal -- lets the one-page trim below drop by id
        })
    return roles


def _group_selected(priority_items, user_email):
    """priority_items: [{"row_id", "text", "kind": "role"|"project"}, ...],
    already decided (which items, in what priority order) by the one LLM
    call upstream -- this just looks up the real company/role/dates/name
    for each row and groups for rendering. `text` may be a truthful reword
    of the point-bank original (the selection step is allowed to do that);
    everything else -- which role/project it belongs to, its dates --
    comes from the real row, never from the LLM.

    Returns (roles, projects), each in the shape resume_render expects,
    ordered by first appearance in priority_items (highest-value role/
    project leads), each group's own items kept in natural point-bank
    order (id order) so it still reads as a coherent timeline rather than
    a priority-sorted jumble -- same rule _group_into_roles already used."""
    all_rows_by_id = {r["id"]: r for r in db.point_bank_all(user_email, active_only=False)}

    def group(items, key_fn):
        by_key, seen_keys = {}, []
        for it in items:
            row = all_rows_by_id.get(it["row_id"])
            if row is None:   # LLM referenced a row that no longer exists -- skip, don't crash
                continue
            key = key_fn(row)
            if key not in seen_keys:
                seen_keys.append(key)
            by_key.setdefault(key, []).append((row, it["text"]))
        return by_key, seen_keys

    def _current_title_row(rows):
        """Which row's `role` value represents the CURRENT title, for a
        company/project with more than one on file (e.g. a promotion).
        Prefer an open-ended row (end_date is None -- "still going") over
        a closed one; among those, the latest start_date; among ties, the
        highest id (most recently added). Never picks by start_date alone
        first -- a genuinely current role with a blank/missing start_date
        in the master doc (a real data gap, not a reason to show a stale
        title instead) must still win over an old, fully-dated role."""
        return max(rows, key=lambda r: (r["end_date"] is None, r["start_date"] or "", r["id"]))

    role_items = [it for it in priority_items if it["kind"] == "role"]
    # Grouped by COMPANY ONLY -- a title change/promotion at the same
    # company (different point-bank `role` values, same company) used to
    # render as two separate top-level blocks, each repeating the company
    # name. One company, one block, even across a promotion.
    by_role, role_keys = group(role_items, lambda r: r["company"])
    roles = []
    for company in role_keys:
        items = sorted(by_role[company], key=lambda pair: pair[0]["id"])
        rows = [row for row, _ in items]
        starts = [r["start_date"] for r in rows if r["start_date"]]
        start = min(starts) if starts else ""
        end = "Present" if any(r["end_date"] is None for r in rows) else max(r["end_date"] for r in rows)
        roles.append({
            "company": company,
            "role": _current_title_row(rows)["role"],
            "dates": f"{start} - {end}",
            "bullets": [text for _, text in items],
            "_row_ids": [row["id"] for row, _ in items],
        })

    project_items = [it for it in priority_items if it["kind"] == "project"]
    # Same consolidation for projects -- a project's tagline/stack line
    # differing slightly across its point-bank rows shouldn't split it
    # into two project blocks either.
    by_proj, proj_keys = group(project_items, lambda r: r["company"])
    projects = []
    for name in proj_keys:
        items = sorted(by_proj[name], key=lambda pair: pair[0]["id"])
        rows = [row for row, _ in items]
        projects.append({
            "name": name,
            "tagline": _current_title_row(rows)["role"],
            "bullets": [text for _, text in items],
            "_row_ids": [row["id"] for row, _ in items],
        })

    return roles, projects


def _page_count(pdf_path):
    out = subprocess.run(["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15)
    for line in out.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":")[1].strip())
    return None


def _convert_to_pdf(docx_path, folder, timeout=120):
    """soffice --headless --convert-to pdf, with its OWN throwaway user
    profile directory every single call. Without -env:UserInstallation,
    every invocation -- across every request, forever -- shares the one
    default LibreOffice profile and its lock file. Found live on the VPS:
    an earlier too-short outer timeout killed a generate_cli.py subprocess
    mid-conversion, but Python's subprocess.run(timeout=) only kills its
    DIRECT child -- the soffice process generate_cli.py had itself spawned
    was orphaned, kept running, and kept holding that shared lock, quietly
    hanging every later conversion (even in unrelated requests) behind it.
    An isolated profile per call means no process, orphaned or not, can
    ever block another one -- the actual fix, not just a longer timeout."""
    profile_dir = os.path.join(tempfile.gettempdir(), f"lo_profile_{uuid.uuid4().hex}")
    try:
        subprocess.run(
            ["soffice", "--headless", f"-env:UserInstallation=file://{profile_dir}",
             "--convert-to", "pdf", "--outdir", folder, docx_path],
            capture_output=True, timeout=timeout)
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def _render_and_count(profile, resume_title, summary, roles, skills_by_category, folder, projects=None):
    docx_path = os.path.join(folder, "resume.docx")
    pdf_path = os.path.join(folder, "resume.pdf")
    resume_render.render(profile, resume_title, summary, roles, skills_by_category, docx_path, projects=projects)
    _convert_to_pdf(docx_path, folder)
    return docx_path, pdf_path, _page_count(pdf_path)


def _lowest_value_row_id(roles, projects, priority_items, protected_ids=frozenset()):
    """The single item to drop when the resume runs over a page: the last
    one still present in priority_items' order, i.e. the lowest-priority
    thing the upstream selection call kept at all -- same "cut from the
    back" rule as before, just driven by the LLM's given order instead of
    a recomputed regex-score delta. Searches roles AND projects together,
    so a weak project can get trimmed before a stronger role bullet, or
    vice versa, purely by priority order.

    protected_ids: row ids the user explicitly forced onto the resume
    (the "Add to resume" project button) -- Call 2 only decides whether a
    forced item is INCLUDED, not where it ranks, so a forced project can
    still land near the back of priority_items and get silently trimmed
    right back out, defeating the whole point of forcing it. Skipped here
    first; only dropped if every other item is already gone (the resume
    genuinely cannot fit even the one thing the user explicitly asked
    for -- a real, visible tradeoff at that point, not a silent one)."""
    present_ids = {rid for role in roles for rid in role["_row_ids"]}
    present_ids |= {rid for proj in projects for rid in proj["_row_ids"]}
    for item in reversed(priority_items):
        if item["row_id"] in present_ids and item["row_id"] not in protected_ids:
            return item["row_id"]
    for item in reversed(priority_items):
        if item["row_id"] in present_ids:
            return item["row_id"]
    return None


def _drop_row(roles, projects, row_id):
    def drop_from(groups):
        out = []
        for g in groups:
            kept = [(b, rid) for b, rid in zip(g["bullets"], g["_row_ids"]) if rid != row_id]
            if kept:
                g = dict(g)
                g["bullets"] = [b for b, _ in kept]
                g["_row_ids"] = [rid for _, rid in kept]
                out.append(g)
        return out
    return drop_from(roles), drop_from(projects)


def generate(job_id, user_email, summary, cover_letter_body, selection, out_root="data/applications"):
    """selection: the one upstream LLM call's decision (platform/server.py's
    _zai_generate_and_score) -- plain data, never computed here:
    {"priority_items": [{"row_id", "text", "kind": "role"|"project"}, ...]
       -- ordered highest-value first, text already truthfully reworded
       where the selection call judged that genuinely applicable,
     "requirements": [{"requirement", "importance", "satisfied", "depth",
       "evidence_row_id"}, ...] -- the per-JD-requirement breakdown,
     "missing": [str, ...] -- real content gaps in the master doc for this JD,
     "score": int, "reason": str}.

    user_email: whose own point_bank/profile/skills this generate pulls
    from, and whose own applications row this writes -- the shared job
    posting (job_id) is the same for everyone, but the resume and cover
    letter are always built from THIS user's own master doc."""
    violations = humanizer.check_human_style(summary) + humanizer.check_human_style(cover_letter_body)
    if violations:
        raise ValueError("Humanizer check failed, nothing generated:\n- " + "\n- ".join(violations))

    job = db.jobs_get(user_email, job_id)
    profile = db.profile_all(user_email)
    skills = db.skills_all(user_email)
    resume_title = profile["title"]

    # A regenerate (new summary/cover letter for a job already drafted but
    # never actually submitted) replaces that draft rather than stacking a
    # second application row for the same job -- applications_add always
    # inserts, nothing dedups by job_id on its own. A job already past
    # 'generated' (really applied) is left alone; this never touches that,
    # it only ever replaces an un-submitted draft.
    #
    # Done FIRST, before any new file gets written: the folder name is
    # slug-plus-date, so a same-day regenerate for the same job produces
    # the exact same path as the draft being replaced. Doing this cleanup
    # AFTER rendering (the original order) deleted the brand-new files
    # along with the old ones, since they shared a path -- caught by
    # actually regenerating the same job twice in one day and watching the
    # output folder vanish, not by reasoning about it.
    old_draft = db.applications_find_draft(user_email, job_id)
    if old_draft:
        db.applications_delete(user_email, old_draft["id"])
        if old_draft["folder_path"] and os.path.isdir(old_draft["folder_path"]):
            shutil.rmtree(old_draft["folder_path"])

    priority_items = selection["priority_items"]
    priority_items, floor_protected_ids = _balance_role_bullets(priority_items, db.point_bank_all(user_email))
    roles, projects = _group_selected(priority_items, user_email)
    # The project is deliberately left OUT of protected_ids -- it's always last in
    # priority_items (role bullets first, "other_items" i.e. the project appended after),
    # so a normal trim pass already reaches it before it would ever touch a role bullet,
    # with no explicit protection needed. Tried explicitly protecting it too (on top of the
    # role floor) and found live that it backfires: once role bullets AND the project were
    # both "protected", a real overflow left nothing in the genuinely-unprotected pool, so
    # the trim's last-resort fallback (which ignores protection once nothing else remains)
    # fired immediately -- and it always picks the project anyway, since the fallback just
    # returns the last item in priority_items regardless of protection. Explicit protection
    # was solving a problem list order already solved, while quietly breaking it.
    protected_ids = frozenset(selection.get("forced_project_ids") or ()) | floor_protected_ids

    # LLM-curated for this JD: only genuinely relevant tagged skills, worded
    # with the JD's own term where that's truthfully the same skill. Falls
    # back to showing every tagged skill (the old behavior) if selection
    # carries no skills list at all -- e.g. a manual /api/jobs/generate call
    # that didn't supply one, so the Skills section is never silently empty.
    llm_skills = selection.get("skills") or []
    skills_by_category = {}
    if llm_skills:
        category_by_name_lower = {s["name"].lower(): s["category"] for s in skills}
        for item in llm_skills:
            name = item.get("name", "")
            category = category_by_name_lower.get(name.lower())
            if category is None:   # not a real tagged skill -- server.py already filters this, belt and suspenders
                continue
            skills_by_category.setdefault(category, []).append(item.get("display") or name)
    else:
        for s in skills:
            skills_by_category.setdefault(s["category"], []).append(s["name"])

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", f"{job['company']}-{job['title']}").strip("-").lower()
    folder = os.path.join(out_root, f"{slug}-{time.strftime('%Y-%m-%d')}")
    os.makedirs(folder, exist_ok=True)

    # One-page enforcement: render, check the REAL rendered page count (not
    # a guessed bullet budget), and if it's over, drop the lowest-priority
    # item(s) (per the upstream selection's own order) and re-render.
    # Repeats until it fits or there's nothing left to drop -- bounded by
    # len(priority_items), so this always terminates. A prompt can't see
    # real DOCX line-wrapping; a renderer can -- this stays deterministic.
    docx_path = pdf_path = None
    pages = None
    for _ in range(len(priority_items) + 1):
        docx_path, pdf_path, pages = _render_and_count(
            profile, resume_title, summary, roles, skills_by_category, folder, projects)
        if pages is None or pages <= MAX_PAGES:
            break
        remaining = sum(len(r["_row_ids"]) for r in roles) + sum(len(p["_row_ids"]) for p in projects)
        if remaining == 0:
            break
        # Drop more than one item per iteration when clearly far over,
        # instead of always exactly one -- measured live: one-at-a-time
        # took 17 real soffice conversions (28.7s) to trim a realistic
        # 25-bullet/2-page resume to 1 page, since each iteration pays
        # LibreOffice's ~1.5-2s process-spawn cost regardless of how small
        # the change was. Rough linear estimate (remaining items currently
        # fill `pages` pages, so ~remaining/pages per page) at 70%
        # strength to stay conservative -- headers/summary/skills are
        # fixed overhead, not bullet-proportional, so a full-strength
        # estimate risks cutting more real content than necessary.
        # Verified against the same real case: 4 iterations/7.0s, landing
        # one item lower than the exact one-at-a-time result (negligible),
        # self-correcting via ordinary one-at-a-time drops as it converges.
        per_page = remaining / pages
        target = math.ceil(per_page * MAX_PAGES)
        excess = remaining - target
        drop_count = max(1, int(excess * 0.7)) if excess > 1 else 1
        # A batch never reaches into protected_ids (the role floor, the one guaranteed
        # project line, any forced project) -- it stops the moment only protected items
        # are left and goes back to the outer loop to re-render and re-measure. Found
        # live: when this estimate overshot, the batch kept calling _lowest_value_row_id
        # without ever re-rendering in between, blew straight through every protected
        # item in one pass, and only THEN re-rendered -- by then the page was well under
        # budget (all that visible empty space) and nothing ever got added back, because
        # the outer loop only ever removes, never restores. One protected item at a time,
        # re-rendering after each, is the existing documented intent for the genuine
        # last-resort case (_lowest_value_row_id's own docstring) -- a batch just wasn't
        # honoring it.
        present_ids = {rid for role in roles for rid in role["_row_ids"]}
        present_ids |= {rid for proj in projects for rid in proj["_row_ids"]}
        unprotected_count = len(present_ids - protected_ids)
        # Capping the batch at the unprotected count stops it reaching into protected
        # content, but doesn't stop it OVERSHOOTING within the unprotected pool itself --
        # found live again: 3 unprotected items existed (2 extra role bullets + the one
        # project), the rough estimate said "cut 3", and it cut all 3 in one pass even
        # though the real overflow only needed 1 removed, leaving the page under-filled
        # with the project gone and no way to add it back. When the unprotected pool is
        # already small, the risk of a bad estimate wasting real content outweighs the
        # soffice-roundtrip cost the batching was trying to save -- go one at a time and
        # re-render between each so the loop can stop the moment it's actually enough.
        safe_drop_count = 1 if unprotected_count <= 5 else min(drop_count, remaining, unprotected_count)
        for _ in range(safe_drop_count):
            drop_id = _lowest_value_row_id(roles, projects, priority_items, protected_ids)
            if drop_id is None:
                break
            roles, projects = _drop_row(roles, projects, drop_id)
            if drop_id in protected_ids:
                # Hit the genuine last-resort case mid-batch -- stop immediately and let
                # the outer loop re-render before even considering dropping a second one.
                break
    one_page_ok = pages is not None and pages <= MAX_PAGES

    # The score/reason/requirement breakdown/missing-content list all come
    # from the one upstream LLM call -- never recomputed here. rendered_pages
    # and one_page_ok are the one thing that call couldn't have known (a
    # physical fact about the real render), added after the fact.
    result = {
        "score": selection["score"],
        "reason": selection.get("reason", ""),
        "requirements": selection.get("requirements", []),
        "project_assessment": selection.get("project_assessment", []),
        "missing": selection.get("missing", []),
        "rendered_pages": pages,
        "one_page_ok": one_page_ok,
    }

    cover_docx = os.path.join(folder, "cover_letter.docx")
    resume_render.render_cover_letter_docx(profile, job["company"], job["title"], cover_letter_body, cover_docx)
    _convert_to_pdf(cover_docx, folder)
    cover_pdf = os.path.join(folder, "cover_letter.pdf")

    with open(os.path.join(folder, "job_description.md"), "w") as f:
        f.write(f"# {job['title']} — {job['company']}\n\n{job['url']}\n\n{job['description_raw']}\n")

    with open(os.path.join(folder, "ats_estimate.md"), "w") as f:
        f.write(f"# ATS estimate: {result['score']}/100\n\n")
        if result["reason"]:
            f.write(f"{result['reason']}\n\n")
        f.write(f"Rendered pages: {pages} "
                f"{'(within the 1-page target)' if one_page_ok else '(OVER 1 page -- ran out of content to trim)'}\n\n")
        f.write("Requirement breakdown (score is the sum of the earned column):\n")
        for req in result["requirements"]:
            status = "OK " if req.get("presence") in ("yes", "semantic") else "GAP"
            pts = f"{req.get('earned', '?')}/{req.get('weight', '?')} pts"
            f.write(f"- [{status}] ({req.get('importance', '')}) {req.get('requirement', '')}"
                    f" -- presence: {req.get('presence', 'n/a')}, depth: {req.get('depth', 'n/a')}, {pts}\n")
        f.write("\n")
        if result["missing"]:
            f.write("Gaps relative to this JD (real, worth addressing in the master doc):\n")
            for m in result["missing"]:
                f.write(f"- {m.get('gap', '')}\n")
                if m.get("why"):
                    f.write(f"  why: {m['why']}\n")
                if m.get("example"):
                    f.write(f"  example that would close it: {m['example']}\n")
        else:
            f.write("Nothing flagged missing from the master doc for this JD.\n")
        if result["project_assessment"]:
            f.write("\nProjects (advisory -- not auto-included; add via the UI if you want one in):\n")
            for pa in result["project_assessment"]:
                f.write(f"- [{'worth adding' if pa.get('worth_including') else 'skip'}] "
                        f"{pa.get('reason', '')}\n")

    db.applications_add(user_email, job_id, pdf_path, cover_pdf, result, folder)
    db.jobs_set_status(user_email, job_id, "generated")

    return {"folder": folder, "ats": result, "roles": roles, "projects": projects,
            "resume_docx": docx_path, "resume_pdf": pdf_path,
            "cover_docx": cover_docx, "cover_pdf": cover_pdf}
