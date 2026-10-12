"""LinkedIn profile verification — multi-scraper fallback + Claude employment verification."""

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

APIFY_BASE_URL = "https://api.apify.com/v2"
SCRAPER_TIMEOUT = 60

LINKEDIN_SCRAPERS = [
    # 1. harvestapi: 4.8★ / 23K users / $4 per 1k / no cookies. Same org we
    #    already trust for posts. Tried first because reliability matters more
    #    than the small price gap vs supreme_coder. Note the unusual input
    #    field — `queries`, not `profileUrls` — and the optional mode flag.
    {
        "name": "harvestapi~linkedin-profile-scraper",
        "input_field": "queries",
        "extra_input": {
            "profileScraperMode": "Profile details no email ($4 per 1k)",
        },
    },
    # 2. dev_fusion: $4 per 1k / no cookies. Solid mid-volume actor.
    {
        "name": "dev_fusion~linkedin-profile-scraper",
        "input_field": "profileUrls",
    },
    # 3. supreme_coder: $3 per 1k / no cookies. Cheapest, kept as last-chance
    #    fallback for cost.
    {
        "name": "supreme_coder~linkedin-profile-scraper",
        "input_field": "urls",
    },
]

VERIFICATION_PROMPT = """You are verifying whether a B2B lead's employment data is current and accurate.

LEAD DATA (from Apollo):
Name: {first_name} {last_name}
Title: {apollo_title}
Company: {apollo_company}

LINKEDIN PROFILE DATA:
Current headline: {linkedin_headline}
Experience history (most recent first):
{experience_list}

RECENT M&A / NEWS CONTEXT:
{news_context}

Determine the lead's employment status. Many professionals have multiple roles simultaneously — \
for example, someone might be a VP at a large company AND a founder of their own startup on the side. \
Also: companies get acquired all the time. When Apollo shows a parent company and LinkedIn shows the \
acquired subsidiary (or vice versa), that's NOT "stale" data — it's a CORRECTION caused by the acquisition.

Respond ONLY with valid JSON:
{{
    "verification_status": "<verified|corrected|post_acquisition|stale|side_role|ambiguous>",
    "primary_role": {{
        "title": "<their main full-time role>",
        "company": "<primary employer>",
        "is_current": true
    }},
    "side_roles": [
        {{
            "title": "<any side business or advisory role>",
            "company": "<side company>",
            "role_type": "<founder|advisor|board_member|consultant>"
        }}
    ],
    "apollo_accuracy": "<accurate|outdated|partial>",
    "explanation": "<brief explanation of the verification result>",
    "recommended_company": "<which company should we reference when reaching out>",
    "recommended_title": "<which title should we use>",
    "outreach_note": "<any context the sales rep should know>"
}}

STATUS DEFINITIONS (PICK CAREFULLY — the dashboard treats these very differently):

- "verified": Apollo and LinkedIn agree. The lead works at the company Apollo says, in the role Apollo says. THE LEAD IS GOOD TO CONTACT AS-IS.

- "corrected": Apollo had outdated or wrong data, but we've identified the right company/title from LinkedIn. The lead is STILL WORKING — just at a different company or with a different title than Apollo had on file. THE LEAD IS GOOD TO CONTACT, just use the corrected company/title.

- "post_acquisition": A specific subset of "corrected" — the discrepancy between Apollo and LinkedIn is explained by a recent acquisition. e.g. "Apollo says Company X, LinkedIn says Company Y, and the news context shows Company Y was acquired by Company X." The lead is still active and the acquisition is itself a buying signal worth referencing in outreach. THE LEAD IS GOOD TO CONTACT.

- "side_role": The Apollo and LinkedIn entries are BOTH currently held by the lead — they have a primary role at one company and a side role (founder/advisor/board) at another. Both are real. THE LEAD IS GOOD TO CONTACT — pick whichever company is more relevant to your sales angle.

- "stale": The lead has GENUINELY LEFT the Apollo company and moved to an entirely unrelated new employer. There is no acquisition or merger story. Apollo's email/phone may no longer reach them. THIS IS THE ONLY STATUS THAT MEANS "DON'T REACH OUT" — use it sparingly and only when you're confident.

- "ambiguous": Can't determine with confidence. LinkedIn might be outdated too, or the profile is sparse.

IMPORTANT RULES:

- If Apollo's company and LinkedIn's first experience entry differ AND the news context shows an acquisition between them, the status MUST be "post_acquisition", not "stale".
- If Apollo's company and LinkedIn's first experience entry differ AND there is NO acquisition explanation, but the lead clearly still holds an active role somewhere, the status is "corrected", not "stale".
- "stale" should ONLY be used when the lead has actually moved on to an unrelated new employer with no connection to Apollo's company.
- If someone is "Founder" of a small startup AND has a full-time role at a larger company, the full-time role is PRIMARY and the startup is a SIDE ROLE.
- A "Founder" title with no other employees visible is likely a side project, not a primary role.

WHEN DATE INFORMATION IS NOT AVAILABLE:
LinkedIn always shows the current/most recent role first in the experience section. Use these heuristics:
- If the FIRST role listed matches the Apollo company, treat as VERIFIED.
- If the FIRST role listed is DIFFERENT from the Apollo company AND the news context explains the difference via M&A, treat as POST_ACQUISITION.
- If the FIRST role listed is DIFFERENT from the Apollo company AND there is no M&A explanation, treat as CORRECTED (not stale — the lead is still active).
- Only use AMBIGUOUS if the profile data is too sparse to determine anything useful (e.g. no experience entries at all)."""


def scrape_linkedin_profile(linkedin_url: str) -> Optional[Dict[str, Any]]:
    """Scrape a LinkedIn profile using multiple Apify actors as fallback.

    Returns the profile data dict or None if all scrapers fail.
    """
    if not settings.APIFY_API_KEY:
        logger.warning("APIFY_API_KEY not set, skipping LinkedIn profile scrape")
        return None

    # Normalise URL
    if not linkedin_url.startswith("https://"):
        if linkedin_url.startswith("http://"):
            linkedin_url = linkedin_url.replace("http://", "https://")
        elif linkedin_url.startswith("www."):
            linkedin_url = "https://" + linkedin_url
        else:
            linkedin_url = "https://www.linkedin.com/in/" + linkedin_url

    for i, scraper in enumerate(LINKEDIN_SCRAPERS):
        logger.info("Trying LinkedIn scraper %d/%d: %s for %s",
                     i + 1, len(LINKEDIN_SCRAPERS), scraper["name"], linkedin_url)
        try:
            input_data = {scraper["input_field"]: [linkedin_url]}
            extras = scraper.get("extra_input")
            if extras:
                input_data.update(extras)
            result = _call_scraper(
                actor_id=scraper["name"],
                input_data=input_data,
            )
            if result and _has_experience_data(result):
                logger.info("LinkedIn profile scraped via %s", scraper["name"])
                return result
            elif result:
                # Got SOMETHING back but no experience keys — log what we did
                # see so we can spot when an actor's schema drifts. Keys-only
                # to avoid dumping a full profile payload at WARN level.
                logger.warning(
                    "Scraper %s returned data without experience keys (got: %s)",
                    scraper["name"],
                    list(result.keys())[:20] if isinstance(result, dict) else type(result).__name__,
                )
                continue
            else:
                logger.warning("Scraper %s returned empty for %s", scraper["name"], linkedin_url)
                continue
        except Exception as e:
            logger.warning("Scraper %s failed: %s", scraper["name"], e)
            continue

    logger.error("All LinkedIn scrapers failed for %s", linkedin_url)
    return None


def _call_scraper(
    actor_id: str,
    input_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Call an Apify LinkedIn profile scraper. Retries once on 429."""
    url = f"{APIFY_BASE_URL}/acts/{actor_id}/run-sync-get-dataset-items"

    for attempt in range(2):
        try:
            with httpx.Client(timeout=SCRAPER_TIMEOUT) as client:
                response = client.post(
                    url,
                    params={"token": settings.APIFY_API_KEY},
                    json=input_data,
                )

                if response.status_code == 429:
                    if attempt == 0:
                        logger.warning("LinkedIn scraper %s rate limited, retrying in 5s", actor_id)
                        time.sleep(5)
                        continue
                    return None

                if response.status_code == 400:
                    logger.warning(
                        "LinkedIn scraper %s returned 400: %s (input: %s)",
                        actor_id, response.text[:300], str(input_data)[:200],
                    )
                    return None

                if not response.is_success:
                    logger.warning(
                        "LinkedIn scraper %s HTTP %d: %s",
                        actor_id, response.status_code, response.text[:300],
                    )
                    return None

                results = response.json()
                if not results or not isinstance(results, list):
                    return None

                # Log raw response structure on first successful call
                first = results[0] if results else {}
                if isinstance(first, dict):
                    logger.info(
                        "LinkedIn scraper %s response keys: %s",
                        actor_id, list(first.keys()),
                    )
                    # Log raw experience structure for debugging
                    for exp_key in ("experience", "experiences", "positions", "workExperience"):
                        exp_val = first.get(exp_key)
                        if isinstance(exp_val, list) and exp_val:
                            sample = exp_val[0] if isinstance(exp_val[0], dict) else exp_val[0]
                            logger.info(
                                "LinkedIn scraper %s experience key '%s' sample: %s",
                                actor_id, exp_key,
                                list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__,
                            )
                            break

                return results[0] if isinstance(first, dict) else None

        except httpx.TimeoutException:
            logger.warning("LinkedIn scraper %s timed out", actor_id)
            return None
        except Exception as e:
            logger.warning("LinkedIn scraper %s error: %s", actor_id, e)
            return None

    return None


def _has_experience_data(profile: Dict[str, Any]) -> bool:
    """Check if profile contains experience/position data."""
    for key in ("experience", "experiences", "positions", "workExperience"):
        val = profile.get(key)
        if isinstance(val, list) and len(val) > 0:
            return True
    return False


def _format_date(value: Any) -> str:
    """Format a date value from various scraper formats into a readable string."""
    if not value:
        return ""

    # Already a string like "2017", "Mar 2011", "2017 - Present"
    if isinstance(value, str):
        return value.strip()

    # Nested dict like {"month": 3, "year": 2011} or {"year": 2017}
    if isinstance(value, dict):
        year = value.get("year") or ""
        month = value.get("month") or ""
        if month and year:
            return f"{month}/{year}"
        return str(year) if year else ""

    # Integer (likely a year or timestamp)
    if isinstance(value, (int, float)):
        # If it looks like a year (1900-2100), use directly
        if 1900 <= value <= 2100:
            return str(int(value))
        return ""

    return str(value)


def _extract_experience_list(profile: Dict[str, Any]) -> str:
    """Extract experience entries into a formatted string for Claude."""
    experiences = []

    # Try multiple possible keys
    raw_exp = None
    for key in ("experience", "experiences", "positions", "workExperience"):
        val = profile.get(key)
        if isinstance(val, list) and val:
            raw_exp = val
            break

    if not raw_exp:
        return "No experience data available"

    any_dates_found = False

    for exp in raw_exp:
        if not isinstance(exp, dict):
            continue

        title = exp.get("title") or exp.get("position") or exp.get("role") or "Unknown"
        company = (
            exp.get("companyName") or exp.get("company") or exp.get("organisation")
            or exp.get("organizationName") or ""
        )
        description = (exp.get("description") or "")[:200]

        # Extract start date — try many possible field names
        start = ""
        for start_key in ("startDate", "start", "startedOn", "from"):
            val = exp.get(start_key)
            if val:
                start = _format_date(val)
                break
        # Try nested dateRange/timePeriod
        if not start:
            date_range = exp.get("dateRange") or exp.get("timePeriod") or {}
            if isinstance(date_range, dict):
                start = _format_date(date_range.get("start") or date_range.get("startDate") or "")
            elif isinstance(date_range, str):
                # Combined string like "Mar 2011 - Jun 2017"
                start = date_range

        # Extract end date
        end = ""
        for end_key in ("endDate", "end", "endedOn", "to"):
            val = exp.get(end_key)
            if val:
                end = _format_date(val)
                break
        if not end:
            date_range = exp.get("dateRange") or exp.get("timePeriod") or {}
            if isinstance(date_range, dict):
                end = _format_date(date_range.get("end") or date_range.get("endDate") or "")

        # Check for combined date string field
        if not start and not end:
            combined_date = exp.get("date") or exp.get("duration") or exp.get("dates") or ""
            if isinstance(combined_date, str) and combined_date:
                start = combined_date
                end = ""

        if not end:
            end = "Present"

        if start:
            any_dates_found = True

        entry = f"- {title} at {company} ({start + ' – ' + end if start else 'dates unknown'})"
        if description:
            entry += f"\n  {description}"
        experiences.append(entry)

    result = "\n".join(experiences) if experiences else "No experience entries found"

    if not any_dates_found and experiences:
        result += (
            "\n\nNOTE: Date information was not available from the LinkedIn scraper. "
            "Do NOT assume roles are concurrent — treat roles listed after the first one "
            "as likely historical unless there is evidence otherwise."
        )

    return result


def verify_lead_employment(
    lead_data: Dict[str, str],
    linkedin_profile: Dict[str, Any],
    recent_news_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Verify lead employment by comparing Apollo data with LinkedIn profile.

    `recent_news_events` is the optional PredictLeads news_events list.
    When supplied, the verifier checks for recent acquisitions/mergers that
    could explain Apollo↔LinkedIn discrepancies — so a contact whose Apollo
    record shows the parent company but whose LinkedIn shows the acquired
    subsidiary gets labelled "post_acquisition" instead of "stale".

    Returns verification dict or a fallback dict on failure.

    Post-process: enriches `primary_role` with `started_at` /
    `ended_at` / `months_in_role` extracted deterministically from the
    raw profile experience entries, by matching primary_role's title +
    company against the raw entries. This unlocks tenure checks
    downstream (e.g. only fire `security_leadership_hire` when the
    person actually started recently; no tenure claim without evidence).
    """
    headline = (
        linkedin_profile.get("headline") or linkedin_profile.get("title")
        or linkedin_profile.get("currentTitle") or ""
    )
    experience_list = _extract_experience_list(linkedin_profile)
    news_context = _format_news_context(recent_news_events)

    prompt = VERIFICATION_PROMPT.format(
        first_name=lead_data.get("first_name", ""),
        last_name=lead_data.get("last_name", ""),
        apollo_title=lead_data.get("title", ""),
        apollo_company=lead_data.get("company_name", ""),
        linkedin_headline=headline,
        experience_list=experience_list,
        news_context=news_context,
    )

    # LinkedIn verification is a structured matching task — does the
    # Apollo lead match the LinkedIn profile we just fetched? Haiku
    # handles this confidently at ~10× lower cost than Sonnet. The
    # downstream business decisions still go through Sonnet (scoring,
    # briefing); this is an upstream gate.
    from app.config import settings as _s
    result = call_claude(prompt, model=_s.CLAUDE_FAST_MODEL)

    if "error" in result:
        logger.warning("Claude verification failed for %s %s: %s",
                        lead_data.get("first_name"), lead_data.get("last_name"), result.get("error"))
        return {
            "verification_status": "unverified",
            "explanation": "Claude verification failed",
            "apollo_accuracy": "unknown",
            "recommended_company": lead_data.get("company_name", ""),
            "recommended_title": lead_data.get("title", ""),
        }

    # Deterministic tenure enrichment — match primary_role to raw
    # experience and pull start/end dates.
    primary_role = result.get("primary_role") or {}
    if isinstance(primary_role, dict) and primary_role.get("title") and primary_role.get("company"):
        tenure = _extract_role_tenure(
            linkedin_profile,
            primary_role.get("title", ""),
            primary_role.get("company", ""),
        )
        if tenure:
            primary_role.update(tenure)
            result["primary_role"] = primary_role

    return result


def _extract_role_tenure(
    profile: Dict[str, Any],
    role_title: str,
    role_company: str,
) -> Optional[Dict[str, Any]]:
    """Find the experience entry matching (role_title, role_company) and
    pull its dates.

    Returns a dict with started_at (ISO YYYY-MM-DD, best-effort),
    ended_at (or None if still current), months_in_role (int or None),
    and started_at_raw (the original date string from the scraper, for
    auditing). Returns None if no match or no dates could be parsed.

    Why deterministic rather than asking Claude: date parsing is the
    one thing LLMs reliably hallucinate, and the consequence here is
    fabricated tenure claims that destroy trust.
    """
    raw_exp = None
    for key in ("experience", "experiences", "positions", "workExperience"):
        val = profile.get(key)
        if isinstance(val, list) and val:
            raw_exp = val
            break
    if not raw_exp:
        return None

    title_lc = (role_title or "").strip().lower()
    company_lc = (role_company or "").strip().lower()

    # Best match: exact (title, company) overlap. Falls back to company-only
    # if title doesn't line up — sometimes LinkedIn shortens titles vs Claude.
    matched = None
    for exp in raw_exp:
        if not isinstance(exp, dict):
            continue
        exp_title = (
            exp.get("title") or exp.get("position") or exp.get("role") or ""
        ).strip().lower()
        exp_company = (
            exp.get("companyName") or exp.get("company")
            or exp.get("organisation") or exp.get("organizationName") or ""
        ).strip().lower()
        if not exp_company:
            continue
        if company_lc and (exp_company in company_lc or company_lc in exp_company):
            # Prefer title-aligned match
            if title_lc and exp_title and (
                exp_title in title_lc or title_lc in exp_title
            ):
                matched = exp
                break
            if matched is None:
                matched = exp
    if matched is None:
        return None

    # Pull dates with the same field-name fallbacks as _extract_experience_list
    start_raw = ""
    for k in ("startDate", "start", "startedOn", "from"):
        v = matched.get(k)
        if v:
            start_raw = _format_date(v)
            break
    if not start_raw:
        date_range = matched.get("dateRange") or matched.get("timePeriod") or {}
        if isinstance(date_range, dict):
            start_raw = _format_date(
                date_range.get("start") or date_range.get("startDate") or ""
            )
        elif isinstance(date_range, str):
            start_raw = date_range

    end_raw = ""
    for k in ("endDate", "end", "endedOn", "to"):
        v = matched.get(k)
        if v:
            end_raw = _format_date(v)
            break

    started_at_iso = _parse_loose_date_to_iso(start_raw) if start_raw else None
    ended_at_iso = _parse_loose_date_to_iso(end_raw) if end_raw else None

    months_in_role = None
    if started_at_iso:
        try:
            from datetime import datetime, timezone
            start_dt = datetime.fromisoformat(started_at_iso).replace(tzinfo=timezone.utc)
            end_dt = (
                datetime.fromisoformat(ended_at_iso).replace(tzinfo=timezone.utc)
                if ended_at_iso
                else datetime.now(timezone.utc)
            )
            delta_days = (end_dt - start_dt).days
            months_in_role = max(0, int(delta_days / 30.44))
        except (ValueError, TypeError):
            pass

    out: Dict[str, Any] = {
        "started_at": started_at_iso,
        "started_at_raw": start_raw or None,
        "ended_at": ended_at_iso,
        "months_in_role": months_in_role,
    }
    # Drop None entries so the dict stays tidy
    return {k: v for k, v in out.items() if v is not None} or None


def _parse_loose_date_to_iso(s: str) -> Optional[str]:
    """Convert LinkedIn's loose date formats to ISO YYYY-MM-DD.

    Handles: 'Mar 2011', '3/2011', '2017', '03/2011', 'March 2011'.
    Day defaults to the 1st of the month. Returns None if unparseable.
    """
    if not s:
        return None
    s = s.strip()
    if s.lower() in ("present", "current", "now"):
        return None
    from datetime import datetime
    for fmt in (
        "%Y-%m-%d", "%Y-%m", "%Y",
        "%b %Y", "%B %Y",
        "%m/%Y", "%-m/%Y",
        "%d/%m/%Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            # %Y alone → assume January
            if fmt == "%Y":
                return dt.strftime("%Y-01-01")
            if fmt in ("%Y-%m", "%b %Y", "%B %Y", "%m/%Y", "%-m/%Y"):
                return dt.strftime("%Y-%m-01")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Last resort: extract a 4-digit year
    import re
    m = re.search(r"\b(19|20)\d{2}\b", s)
    if m:
        return f"{m.group(0)}-01-01"
    return None


def _format_news_context(events: Optional[List[Dict[str, Any]]]) -> str:
    """Format the relevant subset of PredictLeads news events for the verifier.

    We only surface events that could explain a discrepancy between Apollo and
    LinkedIn — acquisitions, mergers, leadership changes, financing rounds.
    Older than 24 months → dropped (the verifier just needs enough context to
    explain "Apollo says X, LinkedIn says Y", not the company's whole history).
    """
    if not events:
        return "(No M&A or news context available — make your decision from LinkedIn vs Apollo alone.)"

    relevant_categories = {
        "acquires", "acquired_by", "merges_with", "merged_with",
        "receives_financing", "raises_funding",
        "hires", "promotes", "departures",
        "partners_with",
    }

    lines: List[str] = []
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=730)  # 24 months

    for ev in events:
        if not isinstance(ev, dict):
            continue
        category = ev.get("category") or ""
        if category not in relevant_categories:
            continue

        # Date filter — keep events within the last 2 years
        date_str = ev.get("date") or ""
        try:
            ev_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if ev_date.tzinfo is None:
                ev_date = ev_date.replace(tzinfo=timezone.utc)
            if ev_date < cutoff:
                continue
            date_label = ev_date.strftime("%b %Y")
        except (ValueError, TypeError):
            date_label = "date unknown"

        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue

        lines.append(f"- [{date_label}] [{category}] {summary}")

    if not lines:
        return "(No relevant M&A or news context within the last 24 months.)"

    return "\n".join(lines)
