#!/usr/bin/env python3
"""Thin CLI wrapper so the merged web server -- running under plain system
python3, which doesn't have python-docx -- can trigger real generation by
shelling out to this venv-python script instead of importing generate.py
directly into its own process.

    .venv/bin/python3 generate_cli.py <request.json>

request.json: {"job_id": int, "user_email": str, "summary": str,
"cover_letter_body": str, "selection": {...}} -- selection is the one
upstream LLM call's content decision (see generate.generate()'s own
docstring for its exact shape), computed in platform/server.py (the only
process with LLM access) and passed through here as plain data, same as
summary/cover_letter_body always were. user_email is whose own master
doc (point_bank/profile/skills) this generate pulls from.
Prints one JSON line to stdout: {"folder": str, "ats": {...}} on success,
{"error": str} on failure (still exit 0 -- the caller checks the "error" key,
not the exit code, so a bad job_id comes back as data, not a stack trace).
"""
import json
import os
import sys

import db
import generate
import validate


def main():
    req = json.load(open(sys.argv[1]))
    user_email = req["user_email"]
    try:
        result = generate.generate(req["job_id"], user_email, req["summary"],
                                     req["cover_letter_body"], req["selection"])
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return

    selection = req["selection"]
    checklist = validate.build_checklist(
        selection["priority_items"], result["roles"], result["projects"],
        req["summary"], req["cover_letter_body"], db.point_bank_all(user_email),
        result["ats"]["rendered_pages"], requirements=selection.get("requirements"),
        max_pages=generate.MAX_PAGES)
    with open(os.path.join(result["folder"], "validation_checklist.md"), "w") as f:
        f.write(validate.render_checklist_md(checklist))

    print(json.dumps({"folder": result["folder"], "ats": result["ats"],
                       "checklist_passed": sum(1 for c in checklist if c["ok"]),
                       "checklist_total": len(checklist)}))


if __name__ == "__main__":
    main()
