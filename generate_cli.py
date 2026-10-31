#!/usr/bin/env python3
"""Runs generate.render_application in the project venv (python-docx lives
there, not in the web server's own python) so the caller can enforce a hard
timeout on the whole render by killing this process group.

    .venv/bin/python3 generate_cli.py <request.json>

request.json: {"job": {...}, "profile": {...}, "skills": [...], "point_bank": [...],
"summary": str, "cover_letter_body": str, "selection": {...}, "out_dir": str}
-- see generate.render_application for the shapes. No database access: every
input arrives as data.

Prints one JSON line: {"ats": {...}, "items": [...], "checklist_passed": n, "checklist_total": n}
on success, {"error": str} on failure. Always exits 0 -- the caller reads the
"error" key, so a bad input comes back as data rather than a stack trace.
"""
import json
import os
import sys

import generate
import validate


def main():
    req = json.load(open(sys.argv[1]))
    try:
        result = generate.render_application(
            req["job"], req["profile"], req["skills"], req["point_bank"], req["summary"],
            req["cover_letter_body"], req["selection"], req["out_dir"])
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return

    selection = req["selection"]
    checklist = validate.build_checklist(
        selection["priority_items"], result["roles"], result["projects"],
        req["summary"], req["cover_letter_body"], req["point_bank"],
        result["ats"]["rendered_pages"], requirements=selection.get("requirements"),
        max_pages=generate.MAX_PAGES)
    with open(os.path.join(req["out_dir"], "validation_checklist.md"), "w") as f:
        f.write(validate.render_checklist_md(checklist))

    items = [{"point_id": rid, "kind": kind, "text": text}
             for kind, groups in (("role", result["roles"]), ("project", result["projects"]))
             for g in groups for text, rid in zip(g["bullets"], g["_row_ids"])]
    print(json.dumps({"ats": result["ats"], "items": items,
                      "checklist_passed": sum(1 for c in checklist if c["ok"]), "checklist_total": len(checklist)}))


if __name__ == "__main__":
    main()
