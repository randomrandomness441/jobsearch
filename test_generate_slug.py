#!/usr/bin/env python3
"""Tests for generate.job_folder_slug -- the folder-name builder behind every
resume/cover-letter generate. Plain stdlib assert, no framework, matching this
project's existing style (see test_jobparse.js).

Covers the real bug reported live: a manually-added job whose company+title
combination produced a folder name over the filesystem's ~255-byte path-
component limit made os.makedirs raise OSError on every single attempt --
the job stuck forever on "Retry generating" because the failure is
deterministic, not transient.

    python3 test_generate_slug.py
"""
import os

import generate

passed = failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"PASS: {name}")
    except Exception as e:
        failed += 1
        print(f"FAIL: {name}\n  {e}")


def _normal():
    slug = generate.job_folder_slug("Gather AI", "Software Development Engineer II")
    assert slug == "gather-ai-software-development-engineer-ii", slug


test("normal company + title produces a short, readable slug", _normal)


def _long_title_at_api_cap():
    # The manual-add API accepts a title up to 200 chars -- on its own this is fine.
    title = "A" * 199
    slug = generate.job_folder_slug("Gather AI", title)
    assert len(slug) <= generate.MAX_SLUG_LEN, len(slug)


test("a 199-char title alone stays within the slug cap", _long_title_at_api_cap)


def _long_company_and_title():
    # THE REPORTED BUG: company has no length check at all, and combined with a
    # near-cap title the resulting folder-name component exceeded 255 bytes.
    company = "International Business Machines Corporation (IBM Global Services Division)"
    title = "A" * 199
    slug = generate.job_folder_slug(company, title)
    import time
    folder_name = f"{slug}-{time.strftime('%Y-%m-%d')}"
    assert len(folder_name.encode()) <= 255, (
        f"folder name must fit the filesystem's path-component limit, got "
        f"{len(folder_name.encode())} bytes: {folder_name!r}")
    assert len(slug) <= generate.MAX_SLUG_LEN


test("long company + long title together stay under the filesystem limit", _long_company_and_title)


def _makedirs_actually_succeeds():
    # Not just a length check on the string -- prove the real syscall this was
    # breaking actually succeeds now, in a throwaway scratch directory.
    import tempfile
    company = "International Business Machines Corporation (IBM Global Services Division)"
    title = "A" * 199
    slug = generate.job_folder_slug(company, title)
    import time
    with tempfile.TemporaryDirectory() as tmp:
        folder = os.path.join(tmp, f"{slug}-{time.strftime('%Y-%m-%d')}")
        os.makedirs(folder, exist_ok=True)
        assert os.path.isdir(folder)


test("os.makedirs with the capped slug actually succeeds (the real failure mode)", _makedirs_actually_succeeds)


def _empty_inputs_dont_throw():
    slug = generate.job_folder_slug("", "")
    assert slug == ""


test("empty company/title does not throw", _empty_inputs_dont_throw)


def _deterministic_same_day_regenerate():
    # generate.py relies on the SAME job producing the SAME slug on a same-day
    # regenerate (so it replaces the old draft folder instead of orphaning it) --
    # the cap must not break that determinism.
    a = generate.job_folder_slug("Gather AI", "Staff Engineer")
    b = generate.job_folder_slug("Gather AI", "Staff Engineer")
    assert a == b


test("slug is deterministic for the same company/title (same-day regenerate relies on this)",
     _deterministic_same_day_regenerate)

print(f"\n{passed} passed, {failed} failed")
exit(0 if failed == 0 else 1)
