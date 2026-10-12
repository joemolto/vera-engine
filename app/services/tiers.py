"""Per-tier policy — the SINGLE source of truth for suggestion caps.

Every ingestion surface (the /discover endpoint, the Vera Brain's
autonomous sourcing, any future path) MUST consult this module rather
than re-deriving the cap locally. Two paths enforcing the cap
differently is exactly the bug that let a Scout-tier org accumulate
20 suggestions instead of 5 — the cap was enforced in /discover but
not in the Brain's store_suggestions path.

If a new client-policy dimension needs enforcing across surfaces, it
belongs in a module like this one — consulted everywhere, defined once.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# How many "suggested" leads each tier maintains as a steady-state cap.
# Calibrated against the tier credit limits so a power user can
# fully consume their credits via suggestions while leaving headroom for
# manual LinkedIn URL searches.
#
# Math:
#   Scout:  100 credits / 20 working days = 5/day  → 5 suggestions/day
#   Growth: 500 credits / 20 working days = 25/day → 20 suggestions/day + 5 manual
#   Scale:  2000 credits / 20 working days = 100/day → 75 suggestions/day + 25 manual
#
# Falls back to 5 if the tier is unknown / not set.
SUGGESTION_TARGETS: Dict[str, int] = {
    "intel": 0,    # free / audit tier (no suggestions, lookups only)
    "scout": 5,
    "growth": 20,
    "scale": 75,
}

DEFAULT_SUGGESTION_TARGET = 5


def get_suggestion_target(db: Any, org_id: str) -> int:
    """Steady-state suggested-lead cap for an org, by its tier.

    Never raises — any failure returns the safe default so a surface
    can't accidentally run uncapped because a lookup hiccuped.
    """
    try:
        org_resp = (
            db.table("organisations")
            .select("tier")
            .eq("id", org_id)
            .limit(1)
            .execute()
        )
        if not org_resp.data:
            return DEFAULT_SUGGESTION_TARGET
        tier = (org_resp.data[0].get("tier") or "scout").lower()
        return SUGGESTION_TARGETS.get(tier, DEFAULT_SUGGESTION_TARGET)
    except Exception as e:
        logger.warning(
            "Failed to read tier for org %s: %s — using default", org_id, e
        )
        return DEFAULT_SUGGESTION_TARGET
