"""Claude-powered alert generation for watchlist changes."""

import logging
from typing import Any, Dict

from app.prompts.watchlist import WATCHLIST_ALERT_PROMPT
from app.services.claude import call_claude

logger = logging.getLogger(__name__)


def _details_with_person(change_data: dict) -> str:
    """Change details, with the person behind the change named explicitly
    when the signal captured one (person_monitor writes person_name), so
    the model can put a name in suggested_action instead of "someone"."""
    details = change_data.get("details", "") or ""
    data = change_data.get("data") or {}
    person = str(data.get("person_name") or change_data.get("person_name") or "").strip()
    if person and person.lower() not in details.lower():
        title = str(data.get("current_title") or data.get("person_title") or "").strip()
        who = f"{person} ({title})" if title else person
        details = f"{details}\nPerson behind this change: {who}"
    return details


def generate_alert(
    change_type: str,
    change_data: Dict[str, Any],
    watchlist_entry: Dict[str, Any],
    org_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate an actionable alert using Claude.

    Args:
        change_type: Type of change detected (e.g. new_job_posting, leadership_change).
        change_data: Details of the change including 'details' and 'data' keys.
        watchlist_entry: The watchlist row dict.
        org_config: Organisation config with name, services, ICP info.

    Returns:
        Dict with alert_title, alert_summary, suggested_action, approach_angle, urgency.
    """
    # Build context from org config
    icp_config = org_config.get("icp_config", {})
    client_company = org_config.get("name", "Our client")
    client_services = icp_config.get("services", "GTM and pipeline services")
    icp_description = icp_config.get("description", "Cybersecurity founders and sales leaders")

    # Build previous state summary from snapshot
    snapshot = watchlist_entry.get("latest_snapshot", {})
    previous_state_parts = []
    if snapshot.get("jobs"):
        previous_state_parts.append(
            f"Jobs: {', '.join(snapshot['jobs'].get('roles_found', []))}"
        )
    if snapshot.get("tech_stack"):
        tech = snapshot["tech_stack"].get("technologies", [])
        previous_state_parts.append(f"Tech stack: {', '.join(tech)}")
    previous_state = "\n".join(previous_state_parts) if previous_state_parts else "No previous data"

    prompt = WATCHLIST_ALERT_PROMPT.format(
        client_company=client_company,
        client_services=client_services,
        icp_description=icp_description,
        target_company=watchlist_entry.get("company_name", "Unknown"),
        target_domain=watchlist_entry.get("company_domain", ""),
        target_contact_name=watchlist_entry.get("target_contact_name", "N/A"),
        target_contact_title=watchlist_entry.get("target_contact_title", "N/A"),
        watch_notes=watchlist_entry.get("notes", "N/A"),
        priority=watchlist_entry.get("priority", "medium"),
        change_type=change_type,
        change_details=_details_with_person(change_data),
        previous_state=previous_state,
    )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Alert generation failed: %s", result.get("error"))
        return {
            "alert_title": f"Change detected: {change_type}",
            "alert_summary": change_data.get("details", "A change was detected."),
            "suggested_action": "Review the change and determine next steps.",
            "approach_angle": "",
            "urgency": "medium",
        }

    return result
