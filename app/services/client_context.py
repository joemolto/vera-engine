"""Shared client context helper — fetches and caches client_context from organisations."""

import logging
from typing import Any, Dict, Optional

from supabase import Client

logger = logging.getLogger(__name__)

# In-memory cache keyed by org_id (lives for the duration of the process/request)
_cache: Dict[str, Dict[str, Any]] = {}


def get_client_context(db: Client, org_id: str) -> Dict[str, Any]:
    """Fetch and cache client context for the session.

    Reads the client_context JSONB column from the organisations table.
    Falls back to building context from icp_config for backwards compatibility.
    Returns a dict that is always safe to use (never None).
    """
    if org_id in _cache:
        return _cache[org_id]

    ctx: Dict[str, Any] = {}
    try:
        org_result = (
            db.table("organisations")
            .select("name, icp_config, client_context")
            .eq("id", org_id)
            .execute()
        )
        if not org_result.data:
            _cache[org_id] = ctx
            return ctx

        org = org_result.data[0]
        client_context = org.get("client_context") or {}
        icp_config = org.get("icp_config") or {}

        # Core identity
        ctx["company_name"] = org.get("name", "")

        # From client_context column (primary source)
        ctx["what_they_sell"] = client_context.get("what_they_sell", "")
        ctx["product_names"] = client_context.get("product_names", [])
        ctx["key_differentiators"] = client_context.get("key_differentiators", "")
        ctx["pain_points_they_solve"] = client_context.get("pain_points_they_solve", "")
        ctx["ideal_triggers"] = client_context.get("ideal_triggers", "")
        ctx["competitors"] = client_context.get("competitors", [])
        ctx["tone_of_voice"] = client_context.get("tone_of_voice", "")
        ctx["disqualify_signals"] = client_context.get("disqualify_signals", "")
        ctx["approach_style"] = client_context.get("approach_style", "")
        ctx["critical_rules"] = client_context.get("critical_rules", "")

        # Commercial context — powers the revenue-potential estimate.
        # All additive keys on the existing client_context JSONB (no
        # migration). Every field is optional: revenue_potential degrades
        # to a safe "unknown" result when average_deal_size is absent.
        ctx["average_deal_size"] = client_context.get("average_deal_size")
        ctx["min_deal_size"] = client_context.get("min_deal_size")
        ctx["max_deal_size"] = client_context.get("max_deal_size")
        ctx["currency"] = client_context.get("currency", "GBP")
        ctx["pricing_model"] = client_context.get("pricing_model", "")
        ctx["typical_sales_cycle_days"] = client_context.get("typical_sales_cycle_days")
        ctx["deal_size_drivers"] = client_context.get("deal_size_drivers", "")
        ctx["expansion_revenue_drivers"] = client_context.get("expansion_revenue_drivers", "")
        ctx["high_value_segments"] = client_context.get("high_value_segments", [])
        ctx["low_value_segments"] = client_context.get("low_value_segments", [])
        ctx["enterprise_threshold_employee_count"] = client_context.get(
            "enterprise_threshold_employee_count"
        )
        ctx["commercial_disqualifiers"] = client_context.get("commercial_disqualifiers", "")

        # Per-client signal-prominence lever. When truthy, threat/fear
        # findings (CVE/breach) are surfaced AFTER opportunity signals so a
        # "never lead with tech/fear" client doesn't get
        # a CVE as its headline finding. Ordering only — strength, scoring
        # and revenue-potential inputs are untouched. Default off.
        ctx["deprioritise_threat_findings"] = client_context.get(
            "deprioritise_threat_findings", False
        )

        # Backwards-compatible fields from icp_config
        ctx["services_offered"] = icp_config.get("services_offered", [])
        ctx["client_positioning"] = icp_config.get("client_positioning", "")
        ctx["client_name"] = org.get("name", "")

        # Discovery-relevant ICP fields used by the discover endpoint's
        # pre-filter (target_locations, target_industries, etc.). These need
        # to be available without re-querying the org row.
        ctx["target_locations"] = icp_config.get("target_locations", [])
        ctx["target_industries"] = icp_config.get("target_industries", [])
        ctx["target_titles"] = icp_config.get("target_titles", [])

        # If what_they_sell is not set, fall back to services_offered
        if not ctx["what_they_sell"] and ctx["services_offered"]:
            ctx["what_they_sell"] = ", ".join(ctx["services_offered"])
        if not ctx["product_names"] and ctx["services_offered"]:
            ctx["product_names"] = ctx["services_offered"]

    except Exception as e:
        logger.warning("Could not fetch client context for %s: %s", org_id, e)

    _cache[org_id] = ctx
    return ctx


def clear_cache(org_id: Optional[str] = None) -> None:
    """Clear cached client context. Pass org_id for a single org, or None for all."""
    if org_id:
        _cache.pop(org_id, None)
    else:
        _cache.clear()


def format_competitors(ctx: Dict[str, Any]) -> str:
    """Format competitors list as a readable string."""
    competitors = ctx.get("competitors", [])
    if isinstance(competitors, list):
        return ", ".join(competitors) if competitors else "N/A"
    return str(competitors) if competitors else "N/A"


def format_product_names(ctx: Dict[str, Any]) -> str:
    """Format product names as a readable string."""
    products = ctx.get("product_names", [])
    if isinstance(products, list):
        return ", ".join(products) if products else "N/A"
    return str(products) if products else "N/A"
