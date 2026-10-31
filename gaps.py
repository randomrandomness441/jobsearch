"""The Gaps tab: group the raw gaps flagged across a user's postings into
distinct underlying weaknesses. One LLM call, run on demand, result cached in
the database (db.gap_clusters_set)."""

import json
import re
import sys

import llm


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
