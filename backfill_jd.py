"""One-time repair: re-fetch every posting from its ATS and overwrite an
existing row's description_raw wherever the fetcher now recovers MORE
content than what's currently stored. Written to fix fetch_lever's old bug
where Lever's separate 'lists' field (the actual "What You'll Do" / "Must
Haves" / "Good to Have" sections) was silently dropped -- description_raw
held only the company blurb for any Lever posting using that field.

Safe to run again later for the same reason: Greenhouse and Ashby already
return the complete JD in one field, so this is a no-op for those; for
Lever rows it only ever overwrites when genuinely more content comes back,
never shrinks a row. Matches fetch_ats.py's own fetch loop (same
companies, same per-source error handling), just backfilling existing rows
instead of inserting new ones.
"""

import time
import urllib.error

import db
from companies import COMPANIES
from fetch_ats import FETCHERS


def main():
    total_checked, total_updated, total_notfound = 0, 0, 0
    for name, ats, slug in COMPANIES:
        try:
            postings = FETCHERS[ats](slug)
        except urllib.error.HTTPError as e:
            print(f"[{ats:10}] {name:20} slug '{slug}' -> HTTP {e.code}")
            continue
        except Exception as e:
            print(f"[{ats:10}] {name:20} slug '{slug}' -> ERROR {e}")
            continue

        updated_here = 0
        for p in postings:
            if not p.get("url") or not p["description_raw"]:
                continue
            total_checked += 1
            existing = db.jobs_find_duplicate(name, p["title"], p["url"], p["description_raw"])
            if not existing:
                total_notfound += 1
                continue
            if len(p["description_raw"]) > len(existing["description_raw"] or ""):
                db.jobs_backfill_description(existing["id"], p["description_raw"])
                updated_here += 1
                total_updated += 1
        if updated_here:
            print(f"[{ats:10}] {name:20} {updated_here} row(s) backfilled with more complete JD text")
        time.sleep(0.5)  # be polite, not just fast -- same pacing as fetch_ats.py's own loop

    print(f"\ntotal: {total_checked} postings checked against existing rows, "
          f"{total_updated} backfilled with more complete JD text, "
          f"{total_notfound} not currently tracked (new or closed since last fetch)")


if __name__ == "__main__":
    main()
