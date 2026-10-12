"""Create the first login + organisation for a self-hosted Vera.

Hosted Vera creates orgs through Stripe checkout; self-hosted has no billing,
so run this once after applying the database schema. It creates (or reuses)
the auth user, creates a Solo-tier org with every Solo feature on, and links
the user as owner. With SELF_HOSTED=true the engine lifts all Solo caps.

Usage (from the engine root, with .env filled in):
    PYTHONPATH=. python scripts/create_org.py --email you@company.com --org "Your Company" --domain company.com

You are prompted for a password. Sign in on the dashboard with these
credentials; you'll be asked to set up two-factor authentication.
"""

import argparse
import getpass

from dotenv import load_dotenv

load_dotenv()

from app.config import settings  # noqa: E402
from app.services.plan_limits import SOLO_FEATURES, SOLO_TIER  # noqa: E402
from supabase import create_client  # noqa: E402


def find_user_id(db, email: str):
    page = 1
    while True:
        users = db.auth.admin.list_users(page=page, per_page=200)
        if not users:
            return None
        for u in users:
            if (u.email or "").lower() == email:
                return u.id
        page += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--org", required=True, help="Organisation name")
    parser.add_argument("--domain", default=None, help="Your company's domain (optional)")
    args = parser.parse_args()

    if not settings.SELF_HOSTED:
        raise SystemExit("Set SELF_HOSTED=true in .env first; without it every Solo cap applies.")

    email = args.email.lower().strip()
    db = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

    user_id = find_user_id(db, email)
    if user_id:
        print(f"Using existing login {email}")
    else:
        password = getpass.getpass("Choose a password (min 8 characters): ")
        if len(password) < 8:
            raise SystemExit("Password too short.")
        created = db.auth.admin.create_user({"email": email, "password": password, "email_confirm": True})
        user_id = created.user.id
        print(f"Created login {email}")

    linked = db.table("org_users").select("org_id").eq("user_id", user_id).limit(1).execute().data
    if linked:
        raise SystemExit(f"{email} already belongs to org {linked[0]['org_id']}; nothing to do.")

    org = db.table("organisations").insert({
        "name": args.org,
        "domain": args.domain,
        "industry": "cybersecurity",
        "tier": SOLO_TIER,
        "subscription_tier": SOLO_TIER,
        "subscription_status": "active",
        "monthly_enrichment_limit": 1_000_000,
        "persona_limit_override": 100,
        "seat_count": 100,
        "icp_config": {"mfa_required": True, "features": dict(SOLO_FEATURES), "signup_email": email},
    }).execute().data[0]

    db.table("org_users").insert({
        "user_id": user_id, "org_id": org["id"], "email": email, "role": "owner",
    }).execute()

    print(f"Created org '{args.org}' ({org['id']}) with {email} as owner. Sign in on the dashboard.")


if __name__ == "__main__":
    main()
