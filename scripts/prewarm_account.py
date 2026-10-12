"""Pre-warm a single account's newest data on the global companies row so a
newly added account renders instantly with everything we've shipped.

Populates (per-domain, global — shared across all orgs):
  1. credential_exposure  — LeakRadar stealer-log / dark-web summary
  2. email_security techs  — vendor names from MX / SPF / DMARC (Mimecast,
                             Proofpoint, Microsoft 365, Dmarcian, …)

Does NOT touch signals or profile_score — those are org-scoped and compute
when the rep pastes the account into their dashboard (enrich + monitor pass).

Usage (with LEAK_RADAR_API_KEY set in the environment or .env):
    PYTHONPATH=. python scripts/prewarm_account.py example.com
"""

import sys

from dotenv import load_dotenv

load_dotenv()

from app.config import settings  # noqa: E402
from supabase import create_client  # noqa: E402
from app.services.enrichers.credential_exposure import check_credential_exposure  # noqa: E402
from app.services.stack_detection.collectors.dns_email import collect_dns  # noqa: E402

if len(sys.argv) < 2:
    raise SystemExit(__doc__)
domain = sys.argv[1].lower().strip()
db = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

co = (
    db.table("companies").select("id,technologies").eq("domain", domain).limit(1).execute().data
)
if not co:
    raise SystemExit(f"No company row for {domain} — enrich it once first.")
cid = co[0]["id"]
techs = co[0].get("technologies") or []
if not isinstance(techs, list):
    techs = []

print(f"Pre-warming {domain} ...")

# 1. Credential exposure (global row).
summary = check_credential_exposure(db, domain, force_refresh=True)
if summary:
    db.table("companies").update({"credential_exposure": summary}).eq("id", cid).execute()
    print(
        f"  credential exposure: employees(90d)={summary.get('recent_employees_90d')} "
        f"third_parties={summary.get('third_parties_compromised')} "
        f"customers={summary.get('customers_compromised')} "
        f"passwords={'yes' if summary.get('employee_passwords_captured') else 'no'}"
    )
else:
    print("  credential exposure: none returned (check LEAK_RADAR_API_KEY)")

# 2. Email-security vendors (global row) — replace the email_security category
#    with fresh collector output, leave all other tech untouched.
kept = [t for t in techs if not (isinstance(t, dict) and t.get("category") == "email_security")]
fresh: list = []
by_name: dict = {}
for d in collect_dns(domain):
    cat = d.category.value if hasattr(d.category, "value") else str(d.category)
    if cat != "email_security":
        continue
    k = d.name.strip().lower()
    if k in by_name:
        by_name[k]["dns_evidence"].append(d.raw_evidence)
        continue
    entry = {
        "name": d.name,
        "category": "email_security",
        "source": "dns",
        "detection_methods": ["dns"],
        "dns_evidence": [d.raw_evidence],
    }
    fresh.append(entry)
    by_name[k] = entry
db.table("companies").update({"technologies": kept + fresh}).eq("id", cid).execute()
print("  email security:", ", ".join(e["name"] for e in fresh) or "—")

print("done")
