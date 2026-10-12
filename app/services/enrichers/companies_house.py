"""Companies House API integration for UK company data."""

import logging
from datetime import datetime, timezone

import httpx
from supabase import Client

from app.config import settings
from app.services.enrichers.cache import get_cached, set_cache
from typing import List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.company-information.service.gov.uk"
REQUEST_TIMEOUT = 10


def enrich_companies_house(
    db: Client,
    company_names: Optional[List[str]] = None,
    company_numbers: Optional[List[str]] = None,
) -> List[dict]:
    """Fetch company data from Companies House API.

    Returns list of enrichment results, one per company found.
    """
    results: List[dict] = []

    # Search by company number first (more precise)
    if company_numbers:
        for number in company_numbers:
            result = _enrich_by_number(db, number)
            if result:
                results.append(result)

    # Then search by name
    if company_names:
        for name in company_names:
            result = _enrich_by_name(db, name)
            if result:
                results.append(result)

    return results


def _enrich_by_name(db: Client, company_name: str) -> Optional[dict]:
    """Search Companies House by name and enrich the best match."""
    # Check cache
    cached = get_cached(db, company_name.lower(), "companies_house")
    if cached:
        return cached

    try:
        auth = httpx.BasicAuth(settings.COMPANIES_HOUSE_API_KEY, "")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(
                f"{BASE_URL}/search/companies",
                params={"q": company_name},
                auth=auth,
            )
            response.raise_for_status()
            search_data = response.json()

        items = search_data.get("items", [])
        if not items:
            return None

        # Use the first (best) match
        company_number = items[0].get("company_number")
        if not company_number:
            return None

        return _fetch_and_parse(db, company_number, company_name)

    except Exception as e:
        logger.error("Companies House search error for '%s': %s", company_name, e)
        return None


def _enrich_by_number(db: Client, company_number: str) -> Optional[dict]:
    """Fetch Companies House data by company number."""
    cached = get_cached(db, company_number, "companies_house")
    if cached:
        return cached

    return _fetch_and_parse(db, company_number)


def _fetch_and_parse(db: Client, company_number: str, cache_key: Optional[str] = None) -> Optional[dict]:
    """Fetch company details, filings, and officers, then parse."""
    try:
        auth = httpx.BasicAuth(settings.COMPANIES_HOUSE_API_KEY, "")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            # Company profile
            company_resp = client.get(f"{BASE_URL}/company/{company_number}", auth=auth)
            company_resp.raise_for_status()
            company_data = company_resp.json()

            # Filing history
            filings_resp = client.get(
                f"{BASE_URL}/company/{company_number}/filing-history",
                params={"items_per_page": 5},
                auth=auth,
            )
            filings = filings_resp.json().get("items", []) if filings_resp.status_code == 200 else []

            # Officers
            officers_resp = client.get(
                f"{BASE_URL}/company/{company_number}/officers",
                auth=auth,
            )
            officers = officers_resp.json().get("items", []) if officers_resp.status_code == 200 else []

        result = _parse_companies_house_data(company_data, filings, officers)

        # Cache by both number and name if available
        set_cache(db, company_number, "companies_house", None, result)
        if cache_key:
            set_cache(db, cache_key.lower(), "companies_house", None, result)

        return result

    except Exception as e:
        logger.error("Companies House fetch error for %s: %s", company_number, e)
        return None


def _parse_companies_house_data(company_data: dict, filings: list, officers: list) -> dict:
    """Parse Companies House API responses into a structured enrichment result."""
    incorporation_date = company_data.get("date_of_creation")
    accounts_info = company_data.get("accounts", {}).get("last_accounts", {})
    accounts_category = accounts_info.get("type", "")

    # Parse current officers (not resigned)
    current_officers = []
    recent_changes = 0
    for o in officers:
        if o.get("resigned_on"):
            continue
        appointed_on = o.get("appointed_on")
        is_recent = _is_within_months(appointed_on, 6) if appointed_on else False
        if is_recent:
            recent_changes += 1
        current_officers.append({
            "name": o.get("name"),
            "role": o.get("officer_role"),
            "appointed_on": appointed_on,
            "is_recent_appointment": is_recent,
        })

    return {
        "company_number": company_data.get("company_number"),
        "company_name": company_data.get("company_name"),
        "incorporation_date": incorporation_date,
        "company_age_years": _years_since(incorporation_date),
        "company_status": company_data.get("company_status"),
        "sic_codes": company_data.get("sic_codes", []),
        "accounts_category": accounts_category,
        "estimated_revenue_bracket": _map_accounts_to_revenue(accounts_category),
        "latest_filing_date": filings[0].get("date") if filings else None,
        "current_officers": current_officers,
        "recent_officer_changes": recent_changes,
        "registered_address": company_data.get("registered_office_address", {}),
    }


def _years_since(date_str: Optional[str]) -> Optional[int]:
    """Calculate years since a date string (YYYY-MM-DD)."""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).days // 365
    except (ValueError, TypeError):
        return None


def _is_within_months(date_str: Optional[str], months: int) -> bool:
    """Check if a date is within the last N months."""
    if not date_str:
        return False
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).days <= months * 30
    except (ValueError, TypeError):
        return False


def _map_accounts_to_revenue(accounts_type: str) -> str:
    """Map Companies House accounts category to estimated revenue bracket."""
    mapping = {
        "micro-entity": "Under £632k",
        "small": "£632k - £10.2m",
        "medium": "£10.2m - £36m",
        "full": "Over £36m",
        "group": "Over £36m",
        "dormant": "Dormant",
    }
    return mapping.get(accounts_type, "Unknown")
