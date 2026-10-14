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
import time

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


def _group_selected(priority_items):
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
    all_rows_by_id = {r["id"]: r for r in db.point_bank_all(active_only=False)}

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

    role_items = [it for it in priority_items if it["kind"] == "role"]
    by_role, role_keys = group(role_items, lambda r: (r["company"], r["role"]))
    roles = []
    for key in role_keys:
        items = sorted(by_role[key], key=lambda pair: pair[0]["id"])
        company, role_title = key
        end = items[0][0]["end_date"] or "Present"
        roles.append({
            "company": company,
            "role": role_title,
            "dates": f"{items[0][0]['start_date']} - {end}",
            "bullets": [text for _, text in items],
            "_row_ids": [row["id"] for row, _ in items],
        })

    project_items = [it for it in priority_items if it["kind"] == "project"]
    by_proj, proj_keys = group(project_items, lambda r: (r["company"], r["role"]))
    projects = []
    for key in proj_keys:
        items = sorted(by_proj[key], key=lambda pair: pair[0]["id"])
        name, tagline = key
        projects.append({
            "name": name,
            "tagline": tagline,
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


def _render_and_count(profile, resume_title, summary, roles, skills_by_category, folder, projects=None):
    docx_path = os.path.join(folder, "resume.docx")
    pdf_path = os.path.join(folder, "resume.pdf")
    resume_render.render(profile, resume_title, summary, roles, skills_by_category, docx_path, projects=projects)
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", folder, docx_path],
                    capture_output=True, timeout=60)
    return docx_path, pdf_path, _page_count(pdf_path)


def _lowest_value_row_id(roles, projects, priority_items):
    """The single item to drop when the resume runs over a page: the last
    one still present in priority_items' order, i.e. the lowest-priority
    thing the upstream selection call kept at all -- same "cut from the
    back" rule as before, just driven by the LLM's given order instead of
    a recomputed regex-score delta. Searches roles AND projects together,
    so a weak project can get trimmed before a stronger role bullet, or
    vice versa, purely by priority order."""
    present_ids = {rid for role in roles for rid in role["_row_ids"]}
    present_ids |= {rid for proj in projects for rid in proj["_row_ids"]}
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


def generate(job_id, summary, cover_letter_body, selection, out_root="data/applications"):
    """selection: the one upstream LLM call's decision (platform/server.py's
    _zai_generate_and_score) -- plain data, never computed here:
    {"priority_items": [{"row_id", "text", "kind": "role"|"project"}, ...]
       -- ordered highest-value first, text already truthfully reworded
       where the selection call judged that genuinely applicable,
     "requirements": [{"requirement", "importance", "satisfied", "depth",
       "evidence_row_id"}, ...] -- the per-JD-requirement breakdown,
     "missing": [str, ...] -- real content gaps in the master doc for this JD,
     "score": int, "reason": str}."""
    violations = humanizer.check_human_style(summary) + humanizer.check_human_style(cover_letter_body)
    if violations:
        raise ValueError("Humanizer check failed, nothing generated:\n- " + "\n- ".join(violations))

    job = db.jobs_get(job_id)
    profile = db.profile_all()
    skills = db.skills_all()
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
    old_draft = db.applications_find_draft(job_id)
    if old_draft:
        db.applications_delete(old_draft["id"])
        if old_draft["folder_path"] and os.path.isdir(old_draft["folder_path"]):
            shutil.rmtree(old_draft["folder_path"])

    priority_items = selection["priority_items"]
    roles, projects = _group_selected(priority_items)

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
        for _ in range(min(drop_count, remaining)):
            drop_id = _lowest_value_row_id(roles, projects, priority_items)
            if drop_id is None:
                break
            roles, projects = _drop_row(roles, projects, drop_id)
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
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", folder, cover_docx],
                    capture_output=True, timeout=60)
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

    db.applications_add(job_id, pdf_path, cover_pdf, result, folder)
    db.jobs_set_status(job_id, "generated")

    return {"folder": folder, "ats": result, "roles": roles, "projects": projects,
            "resume_docx": docx_path, "resume_pdf": pdf_path,
            "cover_docx": cover_docx, "cover_pdf": cover_pdf}
