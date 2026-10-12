"""
Groups — AI interpretation of a group name into structured targeting criteria.

The dashboard's smart-group builder calls /interpret; the deterministic matching
itself runs client-side. This is the only AI in Groups: a cheap Haiku call that
reads intent and expands title + industry synonyms (where users slip up). Mounted
under /api/v1/groups with the shared-key dependency, same as the other internal
routers — the dashboard's server route proxies to it.
"""

import logging
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

router = APIRouter()

_SYSTEM = """You convert a group name into structured B2B targeting criteria for a sales/marketing intelligence tool.

Return ONLY a JSON object, no prose, no markdown:
{
  "titles": string[],      // PEOPLE groups: expand the job-title intent into a COMPREHENSIVE list of real matching job titles and common variants. e.g. "CISO" -> ["CISO","Chief Information Security Officer","Head of Information Security","Head of Security","VP Security","VP of Information Security","Director of Security","Security Director"]. Be generous — the user relies on you to catch variants they would forget. Empty array for account groups.
  "industries": string[],  // BOTH group types: if an industry is implied, expand it into the RELATED industries the user likely also wants, lowercase. e.g. "financial services" -> ["financial services","banking","insurance","fintech","capital markets"]; "healthcare" -> ["healthcare","health insurance","pharmaceuticals","medical devices","hospitals"]. Empty array if none implied.
  "regions": string[],     // countries/regions if implied, as substrings that would appear in an HQ country or city ("united states","united kingdom","germany","new york"). Expand shorthand: "US" -> ["united states"], "DACH" -> ["germany","austria","switzerland"], "EMEA" -> the major EMEA countries. Empty array if none implied.
  "min_score": number|null, // ACCOUNT groups only: if the name implies heat/quality ("hot","heating up","top"), a 0-100 threshold (e.g. 50); else null
  "min_employees": number|null, // ACCOUNT groups: lower employee bound if size is implied ("enterprise" -> 1000, "mid-market" -> 200, "200+" -> 200); else null
  "max_employees": number|null, // ACCOUNT groups: upper employee bound if implied ("SMB" -> 200, "startups" -> 50, "under 500" -> 500); else null
  "min_signals_30d": number|null // ACCOUNT groups: if recent activity is implied ("active","moving","recent signals"), a small threshold like 1 or 2; else null
}"""


class InterpretRequest(BaseModel):
    name: str
    entity_type: str = "account"


def _str_list(v: Any, cap: int) -> List[str]:
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, str)][:cap]


@router.post("/interpret")
def interpret_group(req: InterpretRequest) -> Dict[str, Any]:
    """Plain-English group name -> structured targeting criteria."""
    empty: Dict[str, Any] = {
        "titles": [], "industries": [], "regions": [], "region": None,
        "min_score": None, "min_employees": None, "max_employees": None,
        "min_signals_30d": None,
    }
    name = (req.name or "").strip()
    if not name:
        return empty
    entity_type = "people" if req.entity_type == "people" else "account"
    prompt = (
        f"Group type: {'people (contacts)' if entity_type == 'people' else 'accounts (companies)'}\n"
        f"Group name: {name}"
    )
    try:
        parsed = (
            call_claude(prompt=prompt, system=_SYSTEM, model=settings.CLAUDE_FAST_MODEL, max_tokens=700)
            or {}
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("groups.interpret failed for %r: %s", name, e)
        return {**empty, "error": "interpret_failed"}

    def _num(key: str) -> Any:
        v = parsed.get(key)
        return v if isinstance(v, (int, float)) else None

    regions = _str_list(parsed.get("regions"), 20)
    legacy_region = parsed.get("region")
    if not regions and isinstance(legacy_region, str) and legacy_region.strip():
        regions = [legacy_region.strip()]
    return {
        "titles": _str_list(parsed.get("titles"), 40),
        "industries": _str_list(parsed.get("industries"), 20),
        "regions": regions,
        "region": regions[0] if regions else None,  # back-compat
        "min_score": _num("min_score"),
        "min_employees": _num("min_employees"),
        "max_employees": _num("max_employees"),
        "min_signals_30d": _num("min_signals_30d"),
    }
