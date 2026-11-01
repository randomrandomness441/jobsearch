"""Pull postings from official, free, public APIs -- Greenhouse, Lever,
Ashby. No key, no login, no scraping. Verified this session that all three
are genuinely open, unauthenticated endpoints meant for building careers
pages, not paywalled:
  https://developers.greenhouse.io  (boards-api.greenhouse.io)
  https://github.com/lever/postings-api  (api.lever.co)
  https://developers.ashbyhq.com/docs/public-job-posting-api  (api.ashbyhq.com)

Target companies live in companies.py, not here -- edit that file to add or
fix a slug. Every posting goes through db.jobs_add(), which is unique on (company,
the ATS's own listing id), so re-running this on a schedule is safe: an
already-seen posting only gets its last_seen_ts bumped, nothing doubles up.
"""

import concurrent.futures
import datetime
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request

import ats as classify
import db
from companies import COMPANIES

# Everything that decides whether a posting is stored lives here, at fetch
# time, so what is in the database is exactly what should get a resume:
#   location   Bengaluru / Bangalore / Remote          (ats.is_target_location)
#   title      engineering role, not manager tier      (ats.is_wanted)
#   age        posted within MAX_POSTING_AGE_DAYS
# Nothing downstream re-checks any of it.
MAX_POSTING_AGE_DAYS = 30
is_target_location = classify.is_target_location


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "jobsearch/1.0 (personal use)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _clean_html(raw):
    # unescape twice: some boards' JSON has double-encoded entities
    # ("&amp;nbsp;"), so a single pass leaves literal "&nbsp;" text sitting
    # in the JD instead of a real space -- visible once the detail panel's
    # JD box was actually made readable instead of a cramped scrollbox.
    text = html.unescape(html.unescape(raw or ""))
    text = re.sub(r"<[^<]+?>", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def fetch_greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        raw = j.get("content", "")
        # Most boards return hundreds of postings outside India/Bangalore that
        # main()'s own filter below discards anyway -- cleaning HTML for all
        # of them first was 92% wasted work (measured live: 11,486 of 12,520
        # postings across a real fetch across all companies). Check location
        # on the raw text before paying for _clean_html; a posting that's
        # about to be discarded just carries the raw HTML through as
        # description_raw, which main()'s own is_target_location re-check
        # still correctly rejects, same location field either way.
        description_raw = _clean_html(raw) if is_target_location(location, raw) else raw
        out.append({
            "external_id": str(j["id"]),
            "title": j["title"],
            "url": j.get("absolute_url", ""),
            "description_raw": description_raw,
            "location": location,
            # first_published, not updated_at -- updated_at moves every time
            # a company edits an old listing, which would wrongly keep a
            # months-old posting under a "posted within 30 days" cutoff.
            "posted_ts": _parse_iso(j.get("first_published") or j.get("updated_at")),
        })
    return out


def fetch_lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        location = (j.get("categories") or {}).get("location", "")
        raw = j.get("description", "")
        plain = j.get("descriptionPlain")
        if is_target_location(location, plain or raw):
            desc = plain or _clean_html(raw)
            # Lever splits the ACTUAL requirements out of "description" into a
            # separate "lists" array -- "What You'll Do", "Must Haves", "Good to
            # Have", etc. Missing this meant description_raw was just the
            # company blurb with zero real requirements for any posting using
            # Lever's structured sections (confirmed live: FamPay's "SDE 2
            # Infra" posting had an empty description_raw past the "About"
            # paragraph, with every actual requirement sitting in lists that
            # were never read) -- the LLM was scoring against a JD missing its
            # own requirements section entirely.
            sections = "\n\n".join(
                f"{l.get('text', '')}\n{_clean_html(l.get('content', ''))}"
                for l in (j.get("lists") or []) if l.get("content")
            )
            if sections:
                desc = f"{desc}\n\n{sections}" if desc else sections
        else:
            # same reasoning as fetch_greenhouse above -- not kept, skip the
            # cleaning (and the lists work) entirely.
            desc = plain or raw
        created = j.get("createdAt")  # epoch milliseconds
        out.append({"external_id": str(j["id"]), "title": j.get("text", ""), "url": j.get("hostedUrl", ""),
                     "description_raw": desc,
                     "location": location,
                     "posted_ts": created / 1000 if created else None})
    return out


def fetch_ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []):
        location = j.get("location", "")
        raw = j.get("descriptionHtml", "")
        plain = j.get("descriptionPlain")
        # same reasoning as fetch_greenhouse above
        desc = (plain or _clean_html(raw)) if is_target_location(location, plain or raw) else (plain or raw)
        out.append({"external_id": str(j["id"]), "title": j.get("title", ""), "url": j.get("jobUrl", ""),
                     "description_raw": desc,
                     "location": location,
                     "posted_ts": _parse_iso(j.get("publishedAt"))})
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def _fetch_one(row):
    """Runs on the thread pool below -- just the network call, nothing
    touching the db. Returns (name, ats, slug, postings_or_None, error_or_None)."""
    name, ats, slug = row
    try:
        return name, ats, slug, FETCHERS[ats](slug), None
    except Exception as e:
        return name, ats, slug, None, e


def main():
    """Fetch every company, store what passes the filters, bump last_seen on
    what is already tracked. Returns counts for the status line."""
    total = {"new": 0, "seen": 0, "errors": 0, "skipped_location": 0, "skipped_title": 0, "skipped_age": 0}
    cutoff = time.time() - MAX_POSTING_AGE_DAYS * 86400

    # The ~96 boards are independent hosts, so the network calls run on a pool;
    # the database writes below stay in this one thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        fetched = list(ex.map(_fetch_one, COMPANIES))

    for name, ats_name, slug, postings, err in fetched:
        if err is not None:
            if isinstance(err, urllib.error.HTTPError):
                print(f"[{ats_name:10}] {name:20} slug '{slug}' -> HTTP {err.code} (wrong slug, or board closed)")
            else:
                print(f"[{ats_name:10}] {name:20} slug '{slug}' -> ERROR {err}")
            total["errors"] += 1
            continue

        here = {"new": 0, "seen": 0, "skipped_location": 0, "skipped_title": 0, "skipped_age": 0}
        for p in postings:
            if not p["description_raw"]:
                continue
            location = p.get("location", "")
            if not classify.is_target_location(location, p["description_raw"]):
                here["skipped_location"] += 1
            elif not classify.is_wanted(p["title"], location, p["description_raw"]):
                here["skipped_title"] += 1
            # A posting whose date couldn't be read is kept: we can't confirm
            # it's stale, and dropping it silently is worse than an old row.
            elif p.get("posted_ts") is not None and p["posted_ts"] < cutoff:
                here["skipped_age"] += 1
            else:
                _, is_new = db.jobs_add(ats_name, p["external_id"], name, p["title"], p["url"], p["description_raw"],
                                        location, p.get("posted_ts"))
                here["new" if is_new else "seen"] += 1
        for k, v in here.items():
            total[k] += v
        print(f"[{ats_name:10}] {name:20} {len(postings):4} postings -> {here['new']} new, {here['seen']} already tracked, "
              f"{here['skipped_location']} wrong location, {here['skipped_title']} wrong title, "
              f"{here['skipped_age']} older than {MAX_POSTING_AGE_DAYS}d")

    print(f"\ntotal: {total['new']} new, {total['seen']} already tracked, {total['skipped_location']} wrong location, "
          f"{total['skipped_title']} wrong title, {total['skipped_age']} older than {MAX_POSTING_AGE_DAYS}d, "
          f"{total['errors']} sources errored")
    return total


if __name__ == "__main__":
    main()
