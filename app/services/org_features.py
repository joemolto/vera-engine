"""Per-org feature-flag helpers.

Flags live in organisations.icp_config.features (jsonb). Keeping the
read in one place so routers/services don't each re-implement the
nested-dict dance.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.database import get_supabase

logger = logging.getLogger(__name__)


def feature_enabled(icp_config: Optional[Dict[str, Any]], flag: str) -> bool:
    """True when icp_config.features[flag] is truthy. Pure — pass an
    already-loaded icp_config to avoid a DB round-trip."""
    if not isinstance(icp_config, dict):
        return False
    features = icp_config.get("features")
    if not isinstance(features, dict):
        return False
    return bool(features.get(flag))


def org_feature_enabled(org_id: str, flag: str, db=None) -> bool:
    """Fetch the org's icp_config and check one feature flag."""
    db = db or get_supabase()
    try:
        res = (
            db.table("organisations")
            .select("icp_config")
            .eq("id", org_id)
            .maybe_single()
            .execute()
        )
        cfg = (res.data or {}).get("icp_config") if res else None
        return feature_enabled(cfg, flag)
    except Exception as e:
        logger.warning("org_feature_enabled(%s, %s) failed: %s", org_id, flag, e)
        return False


def monitoring_default(org_id: str, icp_config: Optional[Dict[str, Any]] = None) -> bool:
    """Default monitoring_active for a newly added account.

    Decoupled-monitoring orgs (2026-06-11): new accounts are
    *research* (snapshot only) and NOT monitored until the rep commits —
    explicitly toggles Monitor, unlocks a contact, or enables person
    intel. Every other org keeps the legacy monitor-on-add behaviour.
    """
    if icp_config is not None:
        decoupled = feature_enabled(icp_config, "decoupled_monitoring")
    else:
        decoupled = org_feature_enabled(org_id, "decoupled_monitoring")
    return not decoupled  # decoupled → False (research); else True (monitored)
