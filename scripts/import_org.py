"""Import an organisation exported with scripts/export_org.py into this Vera
install (the database in .env). Run it on a fresh install after applying the
migrations; the organisation keeps its id, so all its records link up.

Logins can't be copied between Supabase projects (passwords and
authenticator apps stay behind), so each member gets a new login with a
one-time password, printed at the end. They sign in, change it in Settings,
and set up their authenticator app again. MCP and API keys are re-created
from the dashboard.

Usage (from the engine root; .env points at the TARGET database):
    PYTHONPATH=. python scripts/import_org.py --in ./org-export

Safe to re-run: existing rows are left as they are.
"""

import argparse
import json
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

from app.config import settings  # noqa: E402
from supabase import create_client  # noqa: E402

# Insert order: parents before children.
ORDER = [
    "markets", "upload_batches", "leads", "watchlist", "signals", "watchlist_alerts",
    "threat_alerts", "groups", "group_members", "market_snapshots", "apollo_people_cache",
    "relevance_feedback", "competitor_feed", "content_suggestions", "action_plans",
    "action_outcomes", "lead_outcomes", "replies", "activity_log", "strategy_memory",
    "campaigns", "chat_sessions", "chat_messages", "chat_memories", "webhook_endpoints",
]
# Shared tables: (table, conflict columns). Rows already in the target win.
SHARED = [
    ("company_intel", "company_id,signal_type"),
    ("company_job_postings", "company_id,source,external_id"),
    ("company_threat_surface", "company_id,threat_intel_id"),
    ("enrichment_cache", "domain,enrichment_type,enrichment_mode"),
    ("website_scan_cache", "domain"),
    ("org_industry_cache", "domain"),
    ("tech_category", "name"),
    ("title_relevance", "archetype,title_norm"),
]
USER_COLUMNS = {
    "org_users": ["user_id"], "chat_sessions": ["user_id"], "chat_memories": ["user_id"],
    "groups": ["created_by"], "upload_batches": ["uploaded_by"],
    "action_outcomes": ["given_by_user_id"],
}
BATCH = 500


def load(folder, table):
    path = os.path.join(folder, f"{table}.json")
    return json.load(open(path)) if os.path.exists(path) else []


def write(db, table, rows, on_conflict="id"):
    for i in range(0, len(rows), BATCH):
        db.table(table).upsert(rows[i:i + BATCH], on_conflict=on_conflict, ignore_duplicates=True).execute()


def remap(rows, column, mapping):
    for r in rows:
        if r.get(column) in mapping:
            r[column] = mapping[r[column]]


def existing_by(db, table, key_cols, rows):
    """Map exported ids to ids already in the target for the same natural key."""
    mapping = {}
    for r in rows:
        q = db.table(table).select("id")
        for c in key_cols:
            q = q.eq(c, r[c]) if r.get(c) is not None else q.is_(c, "null")
        hit = q.limit(1).execute().data
        if hit and hit[0]["id"] != r["id"]:
            mapping[r["id"]] = hit[0]["id"]
    return mapping


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="folder", required=True)
    args = ap.parse_args()
    db = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

    org = load(args.folder, "organisations")[0]
    if db.table("organisations").select("id").eq("id", org["id"]).execute().data:
        print(f"Organisation {org['id']} already exists here: re-running to fill any gaps.")
    org.update({
        "subscription_status": "active", "stripe_customer_id": None, "stripe_subscription_id": None,
        "monthly_enrichment_limit": 1_000_000, "persona_limit_override": 100,
        "seat_count": max(org.get("seat_count") or 1, 100),
    })
    write(db, "organisations", [org])

    # Logins: reuse an existing login with the same email, else create one.
    users = {u.email.lower(): u.id for page in [db.auth.admin.list_users(page=p, per_page=1000) for p in (1, 2, 3)] for u in page}
    user_map, passwords = {}, {}
    members = load(args.folder, "org_users")
    for m in members:
        email = m["email"].lower()
        if email not in users:
            pw = secrets.token_urlsafe(12)
            users[email] = db.auth.admin.create_user({"email": email, "password": pw, "email_confirm": True}).user.id
            passwords[email] = pw
        user_map[m["user_id"]] = users[email]

    data = {t: load(args.folder, t) for t in ["org_users", *ORDER, "companies", "threat_intel", *[s[0] for s in SHARED]]}
    for table, cols in USER_COLUMNS.items():
        for c in cols:
            for r in data[table]:
                if r.get(c) and r[c] not in user_map:
                    r[c] = None  # someone no longer in the org
            remap(data[table], c, user_map)
    data["chat_sessions"] = [r for r in data["chat_sessions"] if r["user_id"]]
    data["chat_memories"] = [r for r in data["chat_memories"] if r["user_id"]]
    kept = {r["id"] for r in data["chat_sessions"]}
    data["chat_messages"] = [r for r in data["chat_messages"] if r["session_id"] in kept]
    write(db, "org_users", data["org_users"])

    # Shared parents: reuse rows the target already has.
    company_map = existing_by(db, "companies", ["domain"], data["companies"])
    write(db, "companies", [c for c in data["companies"] if c["id"] not in company_map])
    threat_map = existing_by(db, "threat_intel", ["source", "source_id"], data["threat_intel"])
    write(db, "threat_intel", [t for t in data["threat_intel"] if t["id"] not in threat_map])
    for t in ("leads", "signals", "lead_outcomes", "company_intel", "company_job_postings", "company_threat_surface"):
        remap(data[t], "company_id", company_map)
    for t in ("threat_alerts", "company_threat_surface"):
        remap(data[t], "threat_intel_id", threat_map)

    for table, conflict in SHARED:
        write(db, table, data[table], on_conflict=conflict)
    for table in ORDER:
        write(db, table, data[table])

    print(f"Imported '{org['name']}' ({org['id']}):")
    for t in ["companies", *ORDER]:
        if not data[t]:
            continue
        got = None
        if "org_id" in data[t][0]:
            got = db.table(t).select("id", count="exact").eq("org_id", org["id"]).limit(1).execute().count
        print(f"  {t}: {len(data[t])} exported" + (f", {got} in this install" if got is not None else ""))
    if passwords:
        print("\nNew logins (share privately; each person changes it after signing in):")
        for email, pw in passwords.items():
            print(f"  {email}  {pw}")


if __name__ == "__main__":
    main()
