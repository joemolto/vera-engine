"""Per-org signal visibility.

ONE rule, one source of truth: a signal type whose weight in
icp_config.signal_priorities is 0 is NEVER shown to that org — digest,
why-this-matters, dashboard surfaces, anywhere. Collection is untouched
(the shared cache keeps learning); this is display-side only.

Born from trial feedback 2026-08-14: credential-leak signals are
dark-web intel, not what a vulnerability-management vendor sells — weights
already ranked signals, but every surface still displayed the full set.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


def hidden_signal_types(icp_config: Optional[Dict[str, Any]]) -> Set[str]:
    """Signal types this org must never see (weight exactly 0)."""
    priorities = (icp_config or {}).get("signal_priorities") or {}
    hidden: Set[str] = set()
    for signal_type, weight in priorities.items():
        try:
            if float(weight) == 0.0:
                hidden.add(signal_type)
        except (TypeError, ValueError):
            continue
    return hidden


def fetch_hidden_signal_types(db, org_id: str) -> Set[str]:
    """Convenience for callers without the org's icp_config in hand.
    Fails open (empty set) — a lookup error must never blank a surface."""
    try:
        row = (db.table("organisations").select("icp_config")
               .eq("id", org_id).maybe_single().execute().data or {})
        return hidden_signal_types(row.get("icp_config"))
    except Exception as e:  # noqa: BLE001
        logger.info("hidden-signals lookup failed for %s: %s", org_id, e)
        return set()
