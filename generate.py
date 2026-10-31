"""Render one resume and cover letter to .docx and .pdf from data the caller
already decided on. No database and no LLM in here -- selection, scoring and
prose all happen upstream (pipeline.py) and arrive as plain data, so this can
run in the project venv (python-docx, soffice) as a subprocess with a hard
timeout, and write into whatever directory it's handed.

What it still decides itself is physical: it measures the REAL rendered page
count and trims the lowest-priority item until the resume fits one page,
because a prompt can't see DOCX line-wrapping and a renderer can.
"""

import math
import os
import shutil
import subprocess
import tempfile
import uuid

import humanizer
import resume_render

# Role bullets always total TARGET_TOTAL_ROLE_BULLETS, split between companies
# as 4-6, 5-5, or 6-4 -- never more lopsided, never a different total. The
# upstream selection optimizes purely for JD relevance and, left alone, made a
# resume read as if one company's years barely happened (10 bullets vs 2).
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


MAX_PAGES = 1


def _group_selected(priority_items, point_bank_items):
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
    all_rows_by_id = {r["id"]: r for r in point_bank_items}

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


def render_application(job, profile, skills, point_bank_items, summary, cover_letter_body, selection, out_dir):
    """job: {title, company, url, description_raw}. profile/skills/
    point_bank_items: this user's master document. selection: the upstream
    decision --
      {"priority_items": [{"row_id", "text", "kind": "role"|"project"}, ...]   highest value first,
       "requirements": [...], "missing": [...], "project_assessment": [...],
       "skills": [{"name", "display"}], "forced_project_ids": [...],
       "score": int, "reason": str}
    Writes resume/cover letter (.docx and .pdf), job_description.md and
    ats_estimate.md into out_dir (created if needed). Returns
    {"ats": {...}, "roles", "projects", "resume_pdf", "cover_pdf"}. Raises
    ValueError if the humanizer gate rejects the prose."""
    violations = humanizer.check_human_style(summary) + humanizer.check_human_style(cover_letter_body)
    if violations:
        raise ValueError("Humanizer check failed, nothing generated:\n- " + "\n- ".join(violations))

    resume_title = profile["title"]
    priority_items, floor_protected_ids = _balance_role_bullets(selection["priority_items"], point_bank_items)
    roles, projects = _group_selected(priority_items, point_bank_items)
    # The project is deliberately NOT protected from the trim: it is always last in
    # priority_items, so an ordinary trim pass already reaches it before any role
    # bullet. Protecting it as well left the trim nothing droppable, and its last-
    # resort fallback (which ignores protection) then picked the project anyway.
    protected_ids = frozenset(selection.get("forced_project_ids") or ()) | floor_protected_ids

    # LLM-curated skills, worded with the JD's own term where truthful. Falls back
    # to every tagged skill if the selection carries none, so the section is
    # never silently empty.
    skills_by_category = {}
    if selection.get("skills"):
        category_by_name = {s["name"].lower(): s["category"] for s in skills}
        for item in selection["skills"]:
            category = category_by_name.get(item.get("name", "").lower())
            if category is not None:   # never show a skill the candidate didn't tag
                skills_by_category.setdefault(category, []).append(item.get("display") or item["name"])
    else:
        for s in skills:
            skills_by_category.setdefault(s["category"], []).append(s["name"])

    os.makedirs(out_dir, exist_ok=True)

    # Render, check the REAL page count, drop the lowest-priority item(s), repeat.
    # Bounded by len(priority_items), so it always terminates.
    pages = None
    for _ in range(len(priority_items) + 1):
        docx_path, pdf_path, pages = _render_and_count(
            profile, resume_title, summary, roles, skills_by_category, out_dir, projects)
        if pages is None or pages <= MAX_PAGES:
            break
        remaining = sum(len(r["_row_ids"]) for r in roles) + sum(len(p["_row_ids"]) for p in projects)
        if remaining == 0:
            break
        # Drop several items per pass when far over: each pass costs a ~2s soffice
        # spawn (17 passes took 28.7s for a 25-bullet, 2-page resume). The estimate
        # runs at 70% strength because headers/summary/skills are fixed overhead.
        excess = remaining - math.ceil(remaining / pages * MAX_PAGES)
        drop_count = max(1, int(excess * 0.7)) if excess > 1 else 1
        present_ids = {rid for role in roles for rid in role["_row_ids"]}
        present_ids |= {rid for proj in projects for rid in proj["_row_ids"]}
        unprotected_count = len(present_ids - protected_ids)
        # With few unprotected items a bad estimate would cut real content for no
        # reason, so go one at a time and re-measure between each.
        safe_drop_count = 1 if unprotected_count <= 5 else min(drop_count, remaining, unprotected_count)
        for _ in range(safe_drop_count):
            drop_id = _lowest_value_row_id(roles, projects, priority_items, protected_ids)
            if drop_id is None:
                break
            roles, projects = _drop_row(roles, projects, drop_id)
            if drop_id in protected_ids:
                break   # last resort reached: re-render before dropping a second protected item
    one_page_ok = pages is not None and pages <= MAX_PAGES

    ats = {
        "score": selection["score"], "reason": selection.get("reason", ""),
        "requirements": selection.get("requirements", []),
        "project_assessment": selection.get("project_assessment", []),
        "missing": selection.get("missing", []),
        "rendered_pages": pages, "one_page_ok": one_page_ok,
    }

    cover_docx = os.path.join(out_dir, "cover_letter.docx")
    resume_render.render_cover_letter_docx(profile, job["company"], job["title"], cover_letter_body, cover_docx)
    _convert_to_pdf(cover_docx, out_dir)
    cover_pdf = os.path.join(out_dir, "cover_letter.pdf")
    if not os.path.isfile(pdf_path) or not os.path.isfile(cover_pdf):
        raise RuntimeError("soffice produced no PDF")

    with open(os.path.join(out_dir, "job_description.md"), "w") as f:
        f.write(f"# {job['title']} -- {job['company']}\n\n{job['url']}\n\n{job['description_raw']}\n")

    with open(os.path.join(out_dir, "ats_estimate.md"), "w") as f:
        f.write(f"# ATS estimate: {ats['score']}/100\n\n")
        if ats["reason"]:
            f.write(f"{ats['reason']}\n\n")
        f.write(f"Rendered pages: {pages} "
                f"{'(within the 1-page target)' if one_page_ok else '(OVER 1 page -- ran out of content to trim)'}\n\n")
        f.write("Requirement breakdown (score is the sum of the earned column):\n")
        for req in ats["requirements"]:
            status = "OK " if req.get("presence") in ("yes", "semantic") else "GAP"
            f.write(f"- [{status}] ({req.get('importance', '')}) {req.get('requirement', '')}"
                    f" -- presence: {req.get('presence', 'n/a')}, depth: {req.get('depth', 'n/a')}, "
                    f"{req.get('earned', '?')}/{req.get('weight', '?')} pts\n")
        f.write("\n")
        if ats["missing"]:
            f.write("Gaps relative to this JD (real, worth addressing in the master doc):\n")
            for m in ats["missing"]:
                f.write(f"- {m.get('gap', '')}\n")
                if m.get("why"):
                    f.write(f"  why: {m['why']}\n")
                if m.get("example"):
                    f.write(f"  example that would close it: {m['example']}\n")
        else:
            f.write("Nothing flagged missing from the master doc for this JD.\n")

    return {"ats": ats, "roles": roles, "projects": projects, "resume_pdf": pdf_path, "cover_pdf": cover_pdf}
