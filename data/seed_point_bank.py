"""One-time seed: extract point-bank rows from Sourav's existing resumes.

Lives under data/ (not jobsearch/ root) since it has real personal contact
info hardcoded below -- this is a one-time personal script, not reusable
platform code. Anyone else uses the "Upload a resume" / "Add point
manually" UI instead.

Run once, from the jobsearch/ directory: python3 data/seed_point_bank.py

Source material: ~/Documents/resume.pdf (current, read in full 2026-09-30) and
~/Documents/Sourav_Singh_Resume_old.pdf (an earlier draft of the SAME two roles,
also read in full). Where the two disagree on a number, BOTH versions are kept
as separate rows tagged 'needs_review' rather than silently picking one -- see
the printed summary at the end for exactly which ones need a decision.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

ROWS = [
    # ── LinkedIn, Software Engineer, April 2024 - April 2026 ──────────────
    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Built LinkedIn's company-wide OLAP platform on dbt and Iceberg, turning one SQL "
              "definition into materialized tables, unions and pre-aggregated cubes over ~70 PB "
              "and 7,000+ daily flows.",
         tags=["dbt", "Iceberg", "OLAP", "data platform", "distributed systems", "SQL"],
         metrics=["~70 PB", "7,000+ daily flows"],
         source_note="resume.pdf"),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Migrated 4,000+ live pipelines off bespoke legacy ELT onto it, building and "
              "migrating in parallel on a system powering Tier 0 member-facing metrics such as "
              "job insights and user metrics.",
         tags=["ELT", "migration", "data platform", "reliability"],
         metrics=["4,000+ live pipelines"],
         source_note="resume.pdf"),
    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="[needs_review: earlier draft] Migrated 1K+ complex legacy Azkaban/Hive flows onto "
              "a new dbt-on-Airflow platform, brought in specifically for Spark performance "
              "tuning to reconcile Hive/Spark SQL differences and keep flows within their "
              "runtime SLAs.",
         tags=["Azkaban", "Hive", "dbt", "Airflow", "Spark", "migration"],
         metrics=["1K+ flows"],
         source_note="Sourav_Singh_Resume_old.pdf -- same migration as above, earlier "
                      "in-progress count (1K+ vs. the current 4,000+); keep the current number "
                      "as the real one unless you tell me otherwise."),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Led the engineering response to an org-wide Code Yellow, designing a 4-layer "
              "release-safety system that held zero SLA misses over 3 months, reporting to "
              "CTO-level leadership daily.",
         tags=["incident response", "reliability", "leadership", "CI/CD"],
         metrics=["zero SLA misses", "3 months"],
         source_note="resume.pdf"),
    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="[fuller version, earlier draft] Chosen as sole owner to stabilize a "
              "business-critical reporting pipeline (~4,000 flows/day) during an org-wide Code "
              "Yellow; designed a 4-layer CI/CD framework (golden flows, dark canary validation, "
              "24-hour soak time, integration testing) that held zero hard-SLA misses through "
              "the 3-month mandate.",
         tags=["incident response", "reliability", "CI/CD", "golden flows", "canary deployment"],
         metrics=["~4,000 flows/day", "zero SLA misses", "3 months"],
         source_note="Sourav_Singh_Resume_old.pdf -- names the 4 layers explicitly (golden "
                      "flows, dark canary, 24h soak, integration testing); the current resume's "
                      "version is shorter. Useful detail for a cover letter or interview even if "
                      "the resume bullet stays short."),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Built multi-catalog APIs with partner teams so downstream consumers read Hive and "
              "Iceberg transparently, migrating 11,000+ tables while holding SLA across every "
              "input type.",
         tags=["Hive", "Iceberg", "API design", "migration", "distributed systems"],
         metrics=["11,000+ tables"],
         source_note="resume.pdf"),
    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="[needs_review: conflicting number] Drove the Hive-to-Iceberg Lakehouse migration "
              "for the org's highest-volume table-producing pipeline as the sole engineer "
              "executing it end to end, converting the step that generates 15K+ tables to "
              "Iceberg with zero downstream breakage.",
         tags=["Hive", "Iceberg", "migration", "ownership"],
         metrics=["15K+ tables", "zero downstream breakage"],
         source_note="Sourav_Singh_Resume_old.pdf -- says 15K+ tables where the current resume "
                      "says 11,000+ for what reads like the same migration. Could be different "
                      "scope (total vs. migrated-so-far) or just a drift between drafts -- "
                      "confirm which number is real before using either in a submitted resume."),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Cut the team's overall memory usage 15% by tuning the heaviest Spark pipelines and "
              "reconciling Hive and Spark SQL semantics to hold jobs within SLA.",
         tags=["Spark", "performance tuning", "Hive", "SQL"],
         metrics=["15% memory reduction"],
         source_note="resume.pdf"),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Built a RAG-based duplicate detector (~98% accurate) using embeddings, a vector "
              "database and semantic search over metric definitions, so the platform reuses "
              "existing pipelines instead of recomputing.",
         tags=["RAG", "embeddings", "vector database", "semantic search", "LLM", "AI tooling"],
         metrics=["~98% accurate"],
         source_note="resume.pdf -- not present in the older draft at all; a newer accomplishment."),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Stood up LinkedIn infra's first central testing platform, letting upstream teams "
              "(Spark, Trino, Azkaban) validate changes against golden flows before release; "
              "adopted by 6+ teams.",
         tags=["testing infrastructure", "Spark", "Trino", "Azkaban", "platform engineering"],
         metrics=["6+ teams adopted"],
         source_note="resume.pdf"),

    dict(company="LinkedIn", role="Software Engineer", start_date="2024-04", end_date="2026-04",
         text="Owned the move of LinkedIn's A/B experimentation reporting (the T-Rex team's "
              "system) onto the new platform, decoupling its logic into a standalone library "
              "that cut ~3% of platform storage and memory.",
         tags=["A/B testing", "experimentation", "library design", "performance tuning"],
         metrics=["~3% storage/memory cut"],
         source_note="Merged from both: current resume.pdf's number (~3%) + old draft's system "
                      "name 'T-Rex' -- the internal name might be worth dropping in an interview "
                      "but probably not a public resume."),

    # ── Cashfree Payments, SDE-1, January 2022 - April 2024 ────────────────
    dict(company="Cashfree Payments", role="Software Development Engineer (SDE-1)",
         start_date="2022-01", end_date="2024-04",
         text="Designed and built the reconciliation reporting pipeline (MySQL to Kafka to "
              "Elasticsearch, with ActiveMQ retries) sustaining ~40K messages/sec, cutting report "
              "generation from 5-6 hours to 1-2 minutes for merchants including IRCTC and Swiggy.",
         tags=["Kafka", "Elasticsearch", "MySQL", "ActiveMQ", "data pipeline", "fintech"],
         metrics=["~40K messages/sec", "5-6 hours to 1-2 minutes"],
         source_note="resume.pdf"),
    dict(company="Cashfree Payments", role="Software Development Engineer (SDE-1)",
         start_date="2022-01", end_date="2024-04",
         text="[needs_review: different framing, same system] Designed an industry-leading "
              "reconciliation report with a 100% success rate, downloadable within 5 seconds of "
              "settlement, among the fastest in the fintech industry.",
         tags=["Kafka", "Elasticsearch", "fintech", "reliability"],
         metrics=["100% success rate", "within 5 seconds"],
         source_note="Sourav_Singh_Resume_old.pdf -- reads like the same reconciliation pipeline "
                      "as above, described by outcome (100% success, 5s) instead of by throughput "
                      "(40K msg/sec, 5-6hr to 1-2min). Both could be true simultaneously; decide "
                      "which framing you want to lead with per job."),

    dict(company="Cashfree Payments", role="Software Development Engineer (SDE-1)",
         start_date="2022-01", end_date="2024-04",
         text="[needs_review: conflicting number] Reduced backup time by 90% by re-engineering "
              "the MySQL to Elasticsearch data transfer path.",
         tags=["MySQL", "Elasticsearch", "performance tuning"],
         metrics=["90% reduction"],
         source_note="resume.pdf says 90%; the older draft's equivalent bullet ('Reduced report "
                      "generation time by 95%...') says 95% and frames it as report generation "
                      "time, not backup time. Possibly two different measurements of the same "
                      "change, or drift between drafts -- confirm the real number."),

    dict(company="Cashfree Payments", role="Software Development Engineer (SDE-1)",
         start_date="2022-01", end_date="2024-04",
         text="Built a real-time merchant dashboard serving 15-40 req/sec at p99 200ms latency.",
         tags=["dashboard", "real-time systems", "latency", "performance"],
         metrics=["15-40 req/sec", "p99 200ms"],
         source_note="resume.pdf, confirmed identical in both versions"),

    dict(company="Cashfree Payments", role="Software Development Engineer (SDE-1)",
         start_date="2022-01", end_date="2024-04",
         text="Right-sized Kubernetes resource allocation from observed utilization, cutting "
              "infrastructure cost as part of a company-wide cost-reduction effort.",
         tags=["Kubernetes", "cost optimization", "infrastructure"],
         metrics=[],
         source_note="resume.pdf"),

    dict(company="Cashfree Payments", role="Software Development Engineer (SDE-1)",
         start_date="2022-01", end_date="2024-04",
         text="Built a no-code filtering feature over ~3,000 Elasticsearch fields via REST API, "
              "letting merchants query payment data without engineering support.",
         tags=["Elasticsearch", "REST API", "product engineering"],
         metrics=["~3,000 fields"],
         source_note="resume.pdf, confirmed in both versions"),
]

SKILLS = [
    ("Java", "Languages"), ("Python", "Languages"), ("SQL", "Languages"),
    ("C++", "Languages"),  # present in the old resume's skill list, absent from the current one
    ("Spark", "Data & Distributed Systems"), ("Kafka", "Data & Distributed Systems"),
    ("Iceberg", "Data & Distributed Systems"), ("dbt", "Data & Distributed Systems"),
    ("Airflow", "Data & Distributed Systems"), ("Trino", "Data & Distributed Systems"),
    ("Pinot", "Data & Distributed Systems"), ("Hive", "Data & Distributed Systems"),
    ("Azkaban", "Data & Distributed Systems"),
    ("Spring Boot", "Backend & Infra"), ("REST APIs", "Backend & Infra"),
    ("MySQL", "Backend & Infra"), ("Redis", "Backend & Infra"),
    ("Elasticsearch", "Backend & Infra"), ("ActiveMQ", "Backend & Infra"),
    ("Kubernetes", "Backend & Infra"), ("CI/CD", "Backend & Infra"),
    ("Quarkus", "Backend & Infra"),  # from the old resume, absent from the current one
    ("star and snowflake schema", "Data Modeling"), ("SCD2", "Data Modeling"),
    ("incremental merges", "Data Modeling"), ("schema evolution", "Data Modeling"),
    ("RAG", "AI Tooling"), ("embeddings", "AI Tooling"), ("vector databases", "AI Tooling"),
    ("semantic search", "AI Tooling"), ("agentic workflows", "AI Tooling"),
    ("Claude Code", "AI Tooling"), ("Cursor", "AI Tooling"),
]

PROFILE = {
    "name": "Sourav Singh",
    "title": "Software Engineer, Distributed Systems",
    "phone": "+91 9128215023",
    "email": "sourav1319singh@gmail.com",
    "linkedin": "linkedin.com/in/souravsingh1319",
    "github": "github.com/sourav1319",
    "education": "B.Tech, Electronics and Communications Engineering, "
                 "National Institute of Technology, Jamshedpur, 2018 - 2022",
    "summary_current": "Software engineer with 4+ years building and owning complex distributed "
                        "systems, from real-time backend services to a company-wide OLAP platform "
                        "running ~70 PB across 7,000+ daily flows. Strong on system design, "
                        "performance tuning and reliability under pressure, with growing depth in "
                        "LLM and RAG tooling.",
}


def main():
    for row in ROWS:
        db.point_bank_add(**row)
    for name, category in SKILLS:
        db.skills_add(name, category)
    for key, value in PROFILE.items():
        db.profile_set(key, value)

    all_rows = db.point_bank_all()
    needs_review = [r for r in all_rows if "needs_review" in r["text"]]
    print(f"Seeded {len(all_rows)} point-bank rows, {len(SKILLS)} skills, {len(PROFILE)} profile fields.")
    print(f"\n{len(needs_review)} rows flagged needs_review -- these have a numeric or framing "
          f"conflict between resume drafts and need your call before going into a submitted resume:")
    for r in needs_review:
        print(f"  [id={r['id']}] {r['company']}: {r['text'][:90]}...")


if __name__ == "__main__":
    main()
