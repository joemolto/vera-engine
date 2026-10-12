"""Export one organisation to a folder of JSON files, to move it to another
Vera install with scripts/import_org.py. Read-only on the source database.

Copies the organisation and everything it owns (accounts, contacts, signals,
alerts, markets, groups, unlocked people, chat history, activity), the
company profiles and cached research behind its accounts, and the shared
lookup tables. Leaves out credentials (API and MCP keys, integration tokens),
billing records and logs; keys are re-created on the new install.

Usage (from the engine root; .env points at the SOURCE database):
    PYTHONPATH=. python scripts/export_org.py --org-id <uuid> --out ./org-export

The folder contains personal data (names, emails of contacts): keep it
private and delete it once the import is done.
"""

import argparse
import json
import os

from dotenv import load_dotenv

load_dotenv()

from app.config import settings  # noqa: E402
from supabase import create_client  # noqa: E402

# Tables owned by the org (filtered on org_id).
ORG_TABLES = [
    "markets", "upload_batches", "leads", "watchlist", "signals", "watchlist_alerts",
    "threat_alerts", "groups", "group_members", "market_snapshots", "apollo_people_cache",
    "relevance_feedback", "competitor_feed", "content_suggestions", "action_plans",
    "action_outcomes", "lead_outcomes", "replies", "activity_log", "strategy_memory",
    "campaigns", "chat_sessions", "chat_memories", "webhook_endpoints",
]
# Shared lookups copied whole (small, not org-specific).
SHARED_TABLES = ["tech_category", "title_relevance"]
PAGE = 1000
CHUNK = 100


def fetch_all(make_query):
    rows, start = [], 0
    while True:
        batch = make_query().range(start, start + PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


def fetch_in(db, table, column, values):
    rows, values = [], sorted({v for v in values if v})
    for i in range(0, len(values), CHUNK):
        part = values[i:i + CHUNK]
        rows.extend(fetch_all(lambda: db.table(table).select("*").in_(column, part)))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    db = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    org = db.table("organisations").select("*").eq("id", args.org_id).execute().data
    if not org:
        raise SystemExit(f"No organisation {args.org_id} in the source database.")

    data = {"organisations": org}
    data["org_users"] = db.table("org_users").select("*").eq("org_id", args.org_id).execute().data
    for t in ORG_TABLES:
        data[t] = fetch_all(lambda t=t: db.table(t).select("*").eq("org_id", args.org_id))
    data["chat_messages"] = fetch_in(db, "chat_messages", "session_id", [s["id"] for s in data["chat_sessions"]])

    # Company profiles and research behind the org's accounts.
    domains = {r.get("company_domain") for t in ("watchlist", "leads") for r in data[t]}
    domains = {d.lower() for d in domains if d}
    company_ids = {r.get("company_id") for t in ("leads", "signals", "lead_outcomes") for r in data[t]}
    companies = {c["id"]: c for c in fetch_in(db, "companies", "domain", domains)}
    companies.update({c["id"]: c for c in fetch_in(db, "companies", "id", company_ids - set(companies))})
    data["companies"] = list(companies.values())
    ids = list(companies)
    for t in ("company_intel", "company_job_postings", "company_threat_surface"):
        data[t] = fetch_in(db, t, "company_id", ids)
    threat_ids = {r.get("threat_intel_id") for t in ("threat_alerts", "company_threat_surface") for r in data[t]}
    data["threat_intel"] = fetch_in(db, "threat_intel", "id", threat_ids)
    all_domains = domains | {c["domain"] for c in data["companies"]}
    for t, col in (("enrichment_cache", "domain"), ("website_scan_cache", "domain"), ("org_industry_cache", "domain")):
        data[t] = fetch_in(db, t, col, all_domains)
    for t in SHARED_TABLES:
        data[t] = fetch_all(lambda t=t: db.table(t).select("*"))

    os.makedirs(args.out, exist_ok=True)
    os.chmod(args.out, 0o700)
    for t, rows in data.items():
        with open(os.path.join(args.out, f"{t}.json"), "w") as f:
            json.dump(rows, f, default=str)
    print(f"Exported '{org[0]['name']}' to {args.out}:")
    for t, rows in data.items():
        if rows:
            print(f"  {t}: {len(rows)}")


if __name__ == "__main__":
    main()
