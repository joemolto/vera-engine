"""Controlled vocabularies for Apollo people-search filters.

The dashboard's Settings UI reads from `/icp/vocabularies` (which returns
these lists) so dropdown values are guaranteed to be acceptable Apollo
inputs. The engine's Apollo payload builder also reads from here when
constructing search calls — same source of truth on both ends.

Every value below is what Apollo expects literally; do not localise or
prettify before sending. The dashboard maps to a display label at render
time.
"""

from typing import Dict, List, TypedDict


class Bracket(TypedDict):
    value: str
    label: str


# Apollo person_seniorities — discrete enum. c_suite + founder + owner +
# partner are the "decision maker" cluster the multithread search already
# uses. We expose the full list so customers can target ICs or managers
# explicitly if they want to (rare in cyber sales, useful for sales-tech
# vendors).
SENIORITIES: List[Bracket] = [
    {"value": "c_suite", "label": "C-suite"},
    {"value": "founder", "label": "Founder"},
    {"value": "owner", "label": "Owner"},
    {"value": "partner", "label": "Partner"},
    {"value": "vp", "label": "VP"},
    {"value": "head", "label": "Head"},
    {"value": "director", "label": "Director"},
    {"value": "manager", "label": "Manager"},
    {"value": "senior", "label": "Senior"},
    {"value": "entry", "label": "Entry"},
    {"value": "intern", "label": "Intern"},
]


# Apollo organization_latest_funding_stage_cd — closed list. Names mirror
# Apollo's internal codes (Crunchbase-style). Display labels are friendlier.
FUNDING_STAGES: List[Bracket] = [
    {"value": "pre_seed", "label": "Pre-seed"},
    {"value": "seed", "label": "Seed"},
    {"value": "series_a", "label": "Series A"},
    {"value": "series_b", "label": "Series B"},
    {"value": "series_c", "label": "Series C"},
    {"value": "series_d", "label": "Series D"},
    {"value": "series_e", "label": "Series E"},
    {"value": "series_f_plus", "label": "Series F+"},
    {"value": "private_equity", "label": "Private equity"},
    {"value": "debt_financing", "label": "Debt financing"},
    {"value": "grant", "label": "Grant"},
    {"value": "ipo", "label": "IPO"},
    {"value": "post_ipo_equity", "label": "Post-IPO equity"},
    {"value": "m_and_a", "label": "M&A / Acquired"},
]


# Apollo organization_num_employees_ranges format: "min,max" strings.
# These are the exact buckets Apollo accepts — don't invent custom ones,
# Apollo will silently drop unknown bucket strings.
COMPANY_SIZE_BUCKETS: List[Bracket] = [
    {"value": "1,10", "label": "1-10"},
    {"value": "11,50", "label": "11-50"},
    {"value": "51,200", "label": "51-200"},
    {"value": "201,500", "label": "201-500"},
    {"value": "501,1000", "label": "501-1,000"},
    {"value": "1001,5000", "label": "1,001-5,000"},
    {"value": "5001,10000", "label": "5,001-10,000"},
    {"value": "10001,1000000", "label": "10,001+"},
]


# Annual revenue brackets in USD. Apollo accepts revenue_range[min] /
# revenue_range[max] as open integers — we bracket them on our side so the
# UI stays clean. Engine looks up min/max from `value` at search time.
REVENUE_BRACKETS: List[Bracket] = [
    {"value": "0,1000000", "label": "Under $1M"},
    {"value": "1000000,10000000", "label": "$1M-$10M"},
    {"value": "10000000,50000000", "label": "$10M-$50M"},
    {"value": "50000000,100000000", "label": "$50M-$100M"},
    {"value": "100000000,500000000", "label": "$100M-$500M"},
    {"value": "500000000,1000000000", "label": "$500M-$1B"},
    {"value": "1000000000,100000000000", "label": "$1B+"},
]


# Cyber-vendor-relevant technologies. Passed to Apollo as
# `q_organization_keyword_tags` (free-text keyword matching against the
# organisation record). Phase 2 we may swap to Apollo's
# currently_using_any_of_technology_uids parameter, which needs the
# UID per technology — but we don't have that UID catalogue right now,
# and keyword matching is the working compromise.
#
# Grouped by category for display in the dropdown. The engine doesn't
# care about categories — it sees a flat list — but the UI does.
CYBER_TECH_KEYWORDS: List[Dict[str, str]] = [
    # Cloud platforms
    {"value": "aws", "label": "AWS", "category": "Cloud"},
    {"value": "microsoft azure", "label": "Microsoft Azure", "category": "Cloud"},
    {"value": "google cloud", "label": "Google Cloud", "category": "Cloud"},
    {"value": "oracle cloud", "label": "Oracle Cloud", "category": "Cloud"},
    {"value": "ibm cloud", "label": "IBM Cloud", "category": "Cloud"},
    {"value": "digitalocean", "label": "DigitalOcean", "category": "Cloud"},

    # Identity
    {"value": "okta", "label": "Okta", "category": "Identity"},
    {"value": "microsoft entra", "label": "Microsoft Entra / Azure AD", "category": "Identity"},
    {"value": "ping identity", "label": "Ping Identity", "category": "Identity"},
    {"value": "duo security", "label": "Duo Security", "category": "Identity"},
    {"value": "auth0", "label": "Auth0", "category": "Identity"},
    {"value": "onelogin", "label": "OneLogin", "category": "Identity"},
    {"value": "jumpcloud", "label": "JumpCloud", "category": "Identity"},
    {"value": "cyberark", "label": "CyberArk", "category": "Identity"},
    {"value": "beyondtrust", "label": "BeyondTrust", "category": "Identity"},
    {"value": "delinea", "label": "Delinea", "category": "Identity"},
    {"value": "hashicorp vault", "label": "HashiCorp Vault", "category": "Identity"},

    # MDM
    {"value": "microsoft intune", "label": "Microsoft Intune", "category": "MDM"},
    {"value": "jamf", "label": "JAMF", "category": "MDM"},
    {"value": "kandji", "label": "Kandji", "category": "MDM"},
    {"value": "vmware workspace one", "label": "Workspace ONE", "category": "MDM"},
    {"value": "mosyle", "label": "Mosyle", "category": "MDM"},
    {"value": "hexnode", "label": "Hexnode", "category": "MDM"},

    # EDR / XDR
    {"value": "crowdstrike", "label": "CrowdStrike", "category": "EDR"},
    {"value": "sentinelone", "label": "SentinelOne", "category": "EDR"},
    {"value": "microsoft defender", "label": "Microsoft Defender", "category": "EDR"},
    {"value": "carbon black", "label": "Carbon Black", "category": "EDR"},
    {"value": "sophos", "label": "Sophos", "category": "EDR"},
    {"value": "cybereason", "label": "Cybereason", "category": "EDR"},
    {"value": "cortex xdr", "label": "Cortex XDR", "category": "EDR"},
    {"value": "trend micro", "label": "Trend Micro", "category": "EDR"},

    # SIEM / Logging
    {"value": "splunk", "label": "Splunk", "category": "SIEM"},
    {"value": "elastic", "label": "Elastic / ELK", "category": "SIEM"},
    {"value": "datadog", "label": "Datadog", "category": "SIEM"},
    {"value": "logrhythm", "label": "LogRhythm", "category": "SIEM"},
    {"value": "sumo logic", "label": "Sumo Logic", "category": "SIEM"},
    {"value": "microsoft sentinel", "label": "Microsoft Sentinel", "category": "SIEM"},
    {"value": "securonix", "label": "Securonix", "category": "SIEM"},
    {"value": "exabeam", "label": "Exabeam", "category": "SIEM"},
    {"value": "chronicle", "label": "Google Chronicle", "category": "SIEM"},

    # Email security
    {"value": "proofpoint", "label": "Proofpoint", "category": "Email security"},
    {"value": "mimecast", "label": "Mimecast", "category": "Email security"},
    {"value": "abnormal security", "label": "Abnormal", "category": "Email security"},
    {"value": "avanan", "label": "Avanan", "category": "Email security"},
    {"value": "tessian", "label": "Tessian", "category": "Email security"},
    {"value": "cofense", "label": "Cofense", "category": "Email security"},
    {"value": "ironscales", "label": "IronScales", "category": "Email security"},

    # CASB / SASE / SSE
    {"value": "netskope", "label": "Netskope", "category": "Network security"},
    {"value": "zscaler", "label": "Zscaler", "category": "Network security"},
    {"value": "symantec", "label": "Symantec", "category": "Network security"},
    {"value": "forcepoint", "label": "Forcepoint", "category": "Network security"},
    {"value": "cato networks", "label": "Cato Networks", "category": "Network security"},

    # Firewall / Network
    {"value": "cisco", "label": "Cisco", "category": "Firewall"},
    {"value": "palo alto networks", "label": "Palo Alto Networks", "category": "Firewall"},
    {"value": "fortinet", "label": "Fortinet", "category": "Firewall"},
    {"value": "check point", "label": "Check Point", "category": "Firewall"},
    {"value": "sonicwall", "label": "SonicWall", "category": "Firewall"},
    {"value": "juniper networks", "label": "Juniper Networks", "category": "Firewall"},

    # Vuln / AppSec
    {"value": "tenable", "label": "Tenable", "category": "Vulnerability"},
    {"value": "qualys", "label": "Qualys", "category": "Vulnerability"},
    {"value": "rapid7", "label": "Rapid7", "category": "Vulnerability"},
    {"value": "snyk", "label": "Snyk", "category": "AppSec"},
    {"value": "veracode", "label": "Veracode", "category": "AppSec"},
    {"value": "checkmarx", "label": "Checkmarx", "category": "AppSec"},
    {"value": "github advanced security", "label": "GitHub Advanced Security", "category": "AppSec"},

    # Cloud security
    {"value": "wiz", "label": "Wiz", "category": "Cloud security"},
    {"value": "lacework", "label": "Lacework", "category": "Cloud security"},
    {"value": "orca security", "label": "Orca Security", "category": "Cloud security"},
    {"value": "prisma cloud", "label": "Prisma Cloud", "category": "Cloud security"},
    {"value": "aqua security", "label": "Aqua Security", "category": "Cloud security"},
    {"value": "sysdig", "label": "Sysdig", "category": "Cloud security"},

    # Backup / Resilience
    {"value": "veeam", "label": "Veeam", "category": "Backup"},
    {"value": "commvault", "label": "Commvault", "category": "Backup"},
    {"value": "cohesity", "label": "Cohesity", "category": "Backup"},
    {"value": "rubrik", "label": "Rubrik", "category": "Backup"},

    # GRC / Compliance
    {"value": "vanta", "label": "Vanta", "category": "GRC"},
    {"value": "drata", "label": "Drata", "category": "GRC"},
    {"value": "onetrust", "label": "OneTrust", "category": "GRC"},
    {"value": "auditboard", "label": "AuditBoard", "category": "GRC"},
    {"value": "servicenow grc", "label": "ServiceNow GRC", "category": "GRC"},

    # Productivity / business platforms (signal of "Microsoft house" etc)
    {"value": "microsoft 365", "label": "Microsoft 365", "category": "Productivity"},
    {"value": "google workspace", "label": "Google Workspace", "category": "Productivity"},
    {"value": "salesforce", "label": "Salesforce", "category": "Productivity"},
    {"value": "hubspot", "label": "HubSpot", "category": "Productivity"},
    {"value": "slack", "label": "Slack", "category": "Productivity"},
    {"value": "zoom", "label": "Zoom", "category": "Productivity"},
    {"value": "servicenow", "label": "ServiceNow", "category": "Productivity"},

    # Dev infrastructure
    {"value": "github", "label": "GitHub", "category": "Dev"},
    {"value": "gitlab", "label": "GitLab", "category": "Dev"},
    {"value": "bitbucket", "label": "Bitbucket", "category": "Dev"},
    {"value": "jenkins", "label": "Jenkins", "category": "Dev"},
    {"value": "circleci", "label": "CircleCI", "category": "Dev"},

    # Containers / orchestration
    {"value": "kubernetes", "label": "Kubernetes", "category": "Containers"},
    {"value": "docker", "label": "Docker", "category": "Containers"},
    {"value": "openshift", "label": "OpenShift", "category": "Containers"},

    # Data
    {"value": "snowflake", "label": "Snowflake", "category": "Data"},
    {"value": "databricks", "label": "Databricks", "category": "Data"},
    {"value": "bigquery", "label": "BigQuery", "category": "Data"},
    {"value": "redshift", "label": "Redshift", "category": "Data"},
]


def all_vocabularies() -> Dict[str, object]:
    """Single dict the dashboard's /icp/vocabularies endpoint serialises.

    Returned shape is the one the BuyerProfilesCard frontend consumes —
    each list is sortable + filterable client-side, no further mapping.
    """
    return {
        "seniorities": SENIORITIES,
        "funding_stages": FUNDING_STAGES,
        "company_sizes": COMPANY_SIZE_BUCKETS,
        "revenue_brackets": REVENUE_BRACKETS,
        "technologies": CYBER_TECH_KEYWORDS,
    }


def revenue_min_max(bracket_value: str) -> tuple[int, int]:
    """Parse a revenue bracket value like '1000000,10000000' into (min, max).

    Used by the Apollo payload builder to convert the dropdown value into
    Apollo's `revenue_range[min]` / `revenue_range[max]` form.
    """
    lo, hi = bracket_value.split(",", 1)
    return int(lo), int(hi)
