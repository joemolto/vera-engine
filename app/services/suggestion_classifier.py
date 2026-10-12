"""Cheap LLM classifier for Apollo-returned candidates.

Apollo's `q_organization_keyword_tags` is fuzzy — searching for
"banking, healthcare, insurance" turns up obvious targets (banks,
hospitals) but also media outlets that cover those industries
(Forbes, Bloomberg, Modern Healthcare) and government regulators
(FDIC). A rep dismissing 5 of 10 suggestions every refresh erodes
trust in the tool fast.

This classifier runs each candidate batch through a low-cost LLM
(gpt-4o-mini via OpenRouter) and drops candidates that aren't a
plausible buyer for the rep's selling motion. Cost target: well
under a cent per topup batch.

The seller profile is built from the org's icp_config:
target_industries + (when present) client_context.product_pitch /
sells_what. Without a clear seller description, we fall back to
"a B2B vendor selling to {industries}" which is enough for the
classifier to do basic exclusion of publishers/regulators.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# OpenRouter model — chosen for cost. gpt-4o-mini at ~$0.15/$0.60 per
# million tokens. A 10-candidate batch is ~600 input + 200 output
# tokens = roughly $0.0002 per call. Pool topups happen on accept/
# dismiss + once a day, so total monthly cost stays under a dollar
# per active org.
_MODEL = "openai/gpt-4o-mini"
_TIMEOUT = 30
_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def classify_candidates(
    candidates: List[Dict[str, Any]],
    *,
    seller_profile: str,
) -> List[Dict[str, Any]]:
    """Return only the candidates the classifier thinks are real targets.

    On any failure (no API key, network error, bad JSON response) we
    pass the original list through unchanged — never block topup on
    classifier issues. Better a noisy day than an empty tray.

    Args:
        candidates: list from `apollo_orgs.search_companies`. Each must
            have at minimum `name` and `domain`.
        seller_profile: short description of what the client sells and
            to whom (e.g. "MDR/SOC services to mid-market enterprises
            in financial services, healthcare, energy"). The classifier
            uses this to decide if each candidate is a plausible buyer.
    """
    if not candidates:
        return candidates
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        logger.info("Classifier skipped — OPENROUTER_API_KEY not set")
        return candidates

    # Compact representation — just the name + chips. The model doesn't
    # need the full Apollo payload to decide "is this a buyer or a
    # publisher/regulator?".
    items = [
        {
            "i": idx,
            "name": c.get("name") or "",
            "reasons": c.get("matched_reasons") or [],
        }
        for idx, c in enumerate(candidates)
    ]

    system = (
        "You evaluate B2B sales candidate companies and decide whether each is "
        "a plausible BUYER for the vendor described.\n\n"
        "DROP candidates that match any of these patterns — they show up in "
        "keyword searches but are not buyers:\n"
        "  - Media / trade publications (e.g. 'Insurance Times', 'Modern "
        "Healthcare', 'Forbes', 'Bloomberg News')\n"
        "  - Recruitment / staffing agencies (e.g. 'HFG Insurance "
        "Recruitment', 'Petroplan', 'Energy Job Search', anything with "
        "'Recruitment'/'Staffing'/'Search'/'Talent' in the name unless the "
        "vendor explicitly sells to them)\n"
        "  - Training / education / academies (e.g. 'Retail Banking Academy', "
        "anything with 'Academy'/'Institute'/'University' that isn't a real "
        "university buying enterprise security)\n"
        "  - Government regulators and trade bodies (e.g. FDIC, FCA, "
        "professional associations)\n"
        "  - Other cybersecurity vendors / MSSPs / managed security providers "
        "— the vendor described sells security, so these are peers and "
        "competitors, not buyers (unless the vendor profile explicitly says "
        "it sells to MSSPs)\n"
        "  - Holding companies / shell entities that don't buy operationally\n"
        "  - Charities and non-profits unless explicitly named in the "
        "vendor profile\n"
        "  - Direct competitors named in the vendor profile\n\n"
        "KEEP candidates that look like operating companies in the target "
        "industries with real headcount and revenue (or look like real "
        "businesses even if Apollo returned sparse data).\n\n"
        "Be strict — when in doubt, drop. You MUST return a decision for "
        "EVERY candidate in the input. Reasoning should be one short clause."
    )
    user = (
        f"Vendor sells: {seller_profile}\n\n"
        f"Candidates (JSON):\n{json.dumps(items)}\n\n"
        "Return ONLY a JSON object with this exact shape:\n"
        '{"decisions": [{"i": 0, "keep": true|false, "reason": "..."}, ...]}\n'
        f"You MUST include one decision per candidate (total: {len(items)}). "
        "Use the same `i` indices from the input. No prose, no markdown fences."
    )

    try:
        res = httpx.post(
            _ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                # response_format isn't universally honored across
                # OpenRouter providers but doesn't hurt to ask.
                "response_format": {"type": "json_object"},
            },
            timeout=_TIMEOUT,
        )
        res.raise_for_status()
        body = res.json()
        raw = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning("Suggestion classifier call failed: %s — keeping all", e)
        return candidates

    decisions = _parse_decisions(raw)
    if decisions is None:
        logger.warning("Classifier returned unparseable JSON, keeping all: %s", raw[:200])
        return candidates

    # Map by index back onto candidates. Anything the classifier didn't
    # rule on stays (default-keep is forgiving of model omissions).
    keep_set: set[int] = set(range(len(candidates)))
    drop_log: List[str] = []
    for d in decisions:
        idx = d.get("i")
        if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
            continue
        if d.get("keep") is False:
            keep_set.discard(idx)
            drop_log.append(
                f"  - {candidates[idx].get('name')}: {d.get('reason') or 'no reason given'}"
            )

    if drop_log:
        logger.info(
            "Classifier dropped %d/%d candidates:\n%s",
            len(drop_log), len(candidates), "\n".join(drop_log),
        )
    return [c for i, c in enumerate(candidates) if i in keep_set]


def _parse_decisions(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Pull the decisions array out of the LLM response. Tolerates a
    JSON object wrapping the array (e.g. {"decisions": [...]}) and
    leading/trailing whitespace or fences."""
    if not raw:
        return None
    s = raw.strip()
    # Strip markdown fences if the model added them despite the prompt
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        # Fallback: try to locate the first [ ... ] block
        start = s.find("[")
        end = s.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        # Common shape we now request: {"decisions": [...]}.
        for v in parsed.values():
            if isinstance(v, list):
                return v
        # Edge case: model collapsed and returned only the first
        # decision as a flat object instead of an array. Treat it
        # as a one-element list so we at least classify that one,
        # rather than discarding the whole response.
        if "i" in parsed and "keep" in parsed:
            return [parsed]
    return None


def build_seller_profile(icp_config: Dict[str, Any]) -> str:
    """Compose a short seller-profile string from icp_config. Used as
    the classifier's context for who the rep sells to.

    Order of preference:
      1. client_context.product_pitch / client_context.sells_what
         (set during onboarding as the canonical seller summary)
      2. critical_rules (often describes the selling motion)
      3. Synthetic fallback from target_industries + target_titles
    """
    if not isinstance(icp_config, dict):
        return "a B2B vendor"

    client_context = icp_config.get("client_context")
    if isinstance(client_context, dict):
        # Field-name fallback order matches the historical onboarding
        # shapes. `product_pitch` is the canonical name (what we ask
        # new clients to populate); `sells_what` / `what_they_sell` /
        # `description` are older schemas we honour so future clients
        # don't all need explicit migration.
        for key in ("product_pitch", "sells_what", "what_they_sell", "description"):
            v = client_context.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()

    critical = icp_config.get("critical_rules")
    if isinstance(critical, str) and critical.strip():
        return critical.strip()

    industries = icp_config.get("target_industries") or []
    titles = icp_config.get("target_titles") or []
    if isinstance(industries, list) and industries:
        ind_str = ", ".join(industries[:5])
        title_str = (
            f" to {titles[0]}-type buyers"
            if isinstance(titles, list) and titles
            else ""
        )
        return f"a B2B vendor selling to enterprises in {ind_str}{title_str}"

    return "a B2B vendor"
