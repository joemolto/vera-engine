"""Company domain resolution — Perplexity Sonar (primary), Claude (fallback), naive guess (last resort)."""

import logging
from typing import Optional

import httpx

from app.config import settings
from app.services.claude import call_claude

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15

SONAR_PROMPT = "What is the official company website domain for '{company_name}'? Reply with ONLY the domain name, nothing else. Example: arugacyber.com. If you cannot find it, reply with UNKNOWN."

CLAUDE_PROMPT = """What is the official company website domain for '{company_name}'?

Reply with ONLY the domain name, nothing else. Example: arugacyber.com
If you are not confident, reply with UNKNOWN."""


def clean_domain(raw: str) -> Optional[str]:
    """Clean a raw domain/URL into a bare domain, or None if invalid.

    Strips protocol, www., query params, fragments, paths.
    Public — use at every ingestion point where a domain enters the system.
    """
    cleaned = raw.strip().lower()
    # Strip markdown, quotes, whitespace
    cleaned = cleaned.strip("`\"'* \n\t")
    # Strip protocol
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    # Strip www.
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    # Strip query parameters and fragments
    cleaned = cleaned.split("?")[0].split("#")[0]
    # Strip trailing slashes/paths
    cleaned = cleaned.split("/")[0].strip()

    # Validate: must contain a dot, no spaces, not "unknown"
    if "unknown" in cleaned or "." not in cleaned or " " in cleaned:
        return None
    # Basic sanity: looks like a domain
    if len(cleaned) < 4 or len(cleaned) > 253:
        return None

    return cleaned


def _resolve_via_sonar(company_name: str) -> Optional[str]:
    """Resolve domain using Perplexity Sonar via OpenRouter (real-time web search)."""
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        return None

    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "perplexity/sonar",
                "messages": [
                    {
                        "role": "user",
                        "content": SONAR_PROMPT.format(company_name=company_name),
                    }
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        domain = clean_domain(raw)
        if domain:
            try:
                from app.database import get_supabase
                from app.services.raw_artifacts import persist_raw
                persist_raw(
                    get_supabase(), domain, "perplexity_sonar", data,
                    source_endpoint="domain_resolver",
                )
            except Exception:
                pass
            logger.info("Perplexity Sonar resolved '%s' → %s", company_name, domain)
            return domain

        logger.info("Perplexity Sonar could not resolve domain for '%s': raw=%s", company_name, raw[:100])
        return None

    except Exception as e:
        logger.warning("Perplexity Sonar domain lookup failed for '%s': %s", company_name, e)
        return None


def _resolve_via_claude(company_name: str) -> Optional[str]:
    """Resolve domain using Claude (guess from training data)."""
    try:
        # Domain resolution is a structured one-shot lookup ("Acme Corp"
        # → "acme.com") with no reasoning required. Haiku handles it just
        # as well at ~10× lower cost. cache_system=False because each
        # call is a different company so caching the system prompt would
        # cost more (cache write) than the read discount can recover
        # before the 5-min TTL elapses.
        from app.config import settings as _s
        result = call_claude(
            CLAUDE_PROMPT.format(company_name=company_name),
            model=_s.CLAUDE_FAST_MODEL,
            cache_system=False,
        )
        # call_claude returns parsed JSON, but this prompt returns plain text
        # The JSON parser may wrap it or return an error
        raw = ""
        if isinstance(result, dict):
            if "error" in result:
                # Try the raw text from the error — sometimes parse fails on plain text
                raw = result.get("raw_text", "")
            else:
                # Might have parsed as {"domain": "..."} or similar
                raw = result.get("domain") or str(result)
        elif isinstance(result, str):
            raw = result

        domain = clean_domain(raw)
        if domain:
            logger.info("Claude resolved '%s' → %s", company_name, domain)
            return domain

        return None

    except Exception as e:
        logger.warning("Claude domain lookup failed for '%s': %s", company_name, e)
        return None


def _guess_domain_from_name(company_name: str) -> Optional[str]:
    """Naive guess: strip common suffixes, join words, append .com."""
    if not company_name:
        return None

    cleaned = company_name.lower()
    for suffix in (" ltd", " limited", " inc", " plc", " llc", " gmbh", " group", " co"):
        cleaned = cleaned.replace(suffix, "")

    domain_name = "".join(c for c in cleaned if c.isalnum())
    if domain_name:
        domain = f"{domain_name}.com"
        logger.info("Guessed domain for '%s' → %s", company_name, domain)
        return domain

    return None


def resolve_company_domain(company_name: str) -> str:
    """Resolve a company's domain from its name.

    Fallback chain:
    1. Perplexity Sonar via OpenRouter (real-time web search)
    2. Claude (training data guess)
    3. Naive name → domain guess

    Returns domain string or empty string if all fail.
    """
    if not company_name:
        return ""

    # Tier 1: Perplexity Sonar
    domain = _resolve_via_sonar(company_name)
    if domain:
        return domain

    # Tier 2: Claude
    domain = _resolve_via_claude(company_name)
    if domain:
        return domain

    # Tier 3: Naive guess
    domain = _guess_domain_from_name(company_name)
    return domain or ""


# ---------------------------------------------------------------------------
# Inverse resolution: domain -> company name. Used by the company enricher as
# a fallback when neither PredictLeads nor a domain-matched Apollo record gave
# a trustworthy name — so a pasted URL still resolves to the right company
# instead of a capitalised domain root or a mismatched subsidiary.
# ---------------------------------------------------------------------------

NAME_FROM_DOMAIN_SONAR_PROMPT = (
    "What is the official company or organisation name for the website domain "
    "'{domain}'? Reply with ONLY the company name, nothing else. "
    "Example: Tesco plc. If you cannot find it, reply with UNKNOWN."
)

NAME_FROM_DOMAIN_CLAUDE_PROMPT = """What is the official company or organisation name for the website domain '{domain}'?

Reply with ONLY the company name, nothing else. Example: Tesco plc
If you are not confident, reply with UNKNOWN."""


def _clean_company_name(raw: str) -> Optional[str]:
    """Sanitise an LLM/Sonar name answer, or None if unusable."""
    if not raw:
        return None
    name = raw.strip().strip("`\"'* \n\t")
    if name:
        name = name.splitlines()[0].strip()
    if not name or "unknown" in name.lower():
        return None
    # A real company name isn't a single char or a whole sentence.
    if len(name) < 2 or len(name) > 80:
        return None
    return name


def _resolve_name_via_sonar(domain: str) -> Optional[str]:
    """Resolve a company name from a domain using Perplexity Sonar (web)."""
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        return None
    try:
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "perplexity/sonar",
                "messages": [
                    {"role": "user", "content": NAME_FROM_DOMAIN_SONAR_PROMPT.format(domain=domain)}
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        name = _clean_company_name(raw)
        if name:
            logger.info("Perplexity Sonar resolved %s -> '%s'", domain, name)
            return name
        logger.info("Perplexity Sonar could not name %s: raw=%s", domain, raw[:100])
        return None
    except Exception as e:
        logger.warning("Perplexity Sonar name lookup failed for %s: %s", domain, e)
        return None


def _resolve_name_via_claude(domain: str) -> Optional[str]:
    """Resolve a company name from a domain using Claude (training data)."""
    try:
        from app.config import settings as _s
        result = call_claude(
            NAME_FROM_DOMAIN_CLAUDE_PROMPT.format(domain=domain),
            model=_s.CLAUDE_FAST_MODEL,
            cache_system=False,
        )
        raw = ""
        if isinstance(result, dict):
            raw = result.get("raw_text") or result.get("name") or ""
        elif isinstance(result, str):
            raw = result
        name = _clean_company_name(raw)
        if name:
            logger.info("Claude resolved %s -> '%s'", domain, name)
            return name
        return None
    except Exception as e:
        logger.warning("Claude name lookup failed for %s: %s", domain, e)
        return None


def resolve_company_name_from_domain(domain: str) -> Optional[str]:
    """Resolve a company's official name from its domain.

    Fallback chain: Perplexity Sonar (real-time web) -> Claude (training
    data). Returns the name or None if both fail. Meant for the enricher's
    gap path only — it never runs on the happy path where Apollo/PredictLeads
    already named the company.
    """
    if not domain:
        return None
    return _resolve_name_via_sonar(domain) or _resolve_name_via_claude(domain)


# ---------------------------------------------------------------------------
# Add-time canonicalisation: which domain should a new account be filed
# under? Two live failure modes (Aug 2026):
#   - alias domains that redirect to the real site (getpostman.com →
#     postman.com) — every downstream lookup keyed on the alias misses;
#   - domains Apollo has no coverage for while the same company's data
#     lives under a sibling domain (octopusenergy.com added, Apollo's
#     people live under octopus.energy) — the account looks empty forever.
# ---------------------------------------------------------------------------

_CANON_TIMEOUT = 6


def _registrable_brand(domain: str) -> str:
    """The brand label of a domain: 'octopusenergy' for octopusenergy.com,
    'gt' for us.gt.com, 'acme' for acme.co.uk. Naive public-suffix handling
    (co/com/org/net/ac/gov + 2-letter country) — good enough for a
    same-brand comparison, not for general apex extraction."""
    labels = (domain or "").lower().strip(".").split(".")
    if len(labels) < 2:
        return ""
    if (
        len(labels) >= 3
        and labels[-2] in ("co", "com", "org", "net", "ac", "gov")
        and len(labels[-1]) == 2
    ):
        return labels[-3]
    return labels[-2]


def _same_brand(a: str, b: str) -> bool:
    """octopusenergy ↔ octopus, getpostman ↔ postman: one brand label
    contains the other (≥4 chars so 'it'/'go' can't false-match)."""
    if not a or not b:
        return False
    return a == b or (len(a) >= 4 and a in b) or (len(b) >= 4 and b in a)


def _redirect_target(domain: str) -> Optional[str]:
    """Where does https://{domain} actually land? Returns the final host
    (www-stripped) after following redirects, or None if unreachable.
    HEAD first; some sites reject it, so fall back to GET."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; VeraBot/1.0)"}
    try:
        with httpx.Client(timeout=_CANON_TIMEOUT, follow_redirects=True) as client:
            res = client.head(f"https://{domain}", headers=headers)
            if res.status_code in (403, 405, 400):
                res = client.get(f"https://{domain}", headers=headers)
            host = (res.url.host or "").lower()
    except Exception:  # noqa: BLE001 — DNS failure, TLS error, timeout
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _apollo_knows_domain(domain: str) -> bool:
    """Does Apollo have an organisation on file for this exact domain?
    Non-reveal call. Fail-open: any error returns True so an Apollo outage
    never produces spurious 'did you mean' suggestions."""
    if not settings.APOLLO_API_KEY:
        return True
    try:
        res = httpx.get(
            "https://api.apollo.io/api/v1/organizations/enrich",
            headers={"X-Api-Key": settings.APOLLO_API_KEY},
            params={"domain": domain},
            timeout=_CANON_TIMEOUT,
        )
        if not res.is_success:
            return True
        org = (res.json() or {}).get("organization") or {}
        return bool(org.get("id"))
    except Exception:  # noqa: BLE001
        return True


def _names_equivalent(a: str, b: str) -> bool:
    """Company-name match for the Apollo-blind suggestion: alphanumeric
    casefold, equal or one a prefix of the other ('Octopus Energy' ↔
    'Octopus Energy Group')."""
    na = "".join(c for c in (a or "").lower() if c.isalnum())
    nb = "".join(c for c in (b or "").lower() if c.isalnum())
    if len(na) < 3 or len(nb) < 3:
        return False
    return na == nb or na.startswith(nb) or nb.startswith(na)


def canonicalise_domain(domain: str, company_name: str = "") -> dict:
    """Decide the domain a new account should be filed under.

    Returns {"domain": <use this>, "canonicalised_from": <original|None>,
    "suggestion": {"domain", "name", "reason"} | None}. Never raises.

    Auto-adopts ONLY a same-brand redirect target — the site itself told us
    where it lives. Anything inferred by name matching is returned as a
    suggestion for the caller to surface, never adopted silently: web
    research once offered barricadesystems.com for barricade.ai, and a
    silently swapped domain would file the account under the wrong company.
    """
    original = clean_domain(domain or "") or (domain or "").strip().lower()
    out: dict = {"domain": original, "canonicalised_from": None, "suggestion": None}
    if not original or "." not in original:
        return out

    target = _redirect_target(original)
    if target and target != original and not (
        target.endswith("." + original) or original.endswith("." + target)
    ):
        if _same_brand(_registrable_brand(original), _registrable_brand(target)):
            out["domain"] = target
            out["canonicalised_from"] = original
            return out
        # Cross-brand redirect (acquisition, parked domain) — surface it,
        # don't silently file the account under a different company.
        out["suggestion"] = {
            "domain": target,
            "name": "",
            "reason": f"{original} redirects to {target}",
        }
        return out

    # No redirect. If Apollo is blind to this domain but knows the company
    # by name under a different one, suggest the sibling (octopusenergy.com
    # vs octopus.energy: both live sites, all the people data on the latter).
    if company_name and not _apollo_knows_domain(original):
        try:
            from app.services.apollo_orgs import search_by_name
            for cand in search_by_name(company_name, limit=5):
                cand_domain = (cand.get("domain") or "").lower()
                if (
                    cand_domain
                    and cand_domain != original
                    and _names_equivalent(company_name, cand.get("name") or "")
                ):
                    out["suggestion"] = {
                        "domain": cand_domain,
                        "name": cand.get("name") or "",
                        "reason": (
                            f"Apollo has no data for {original}, but covers "
                            f"'{cand.get('name')}' at {cand_domain} — contact "
                            "discovery will be empty on the domain you added"
                        ),
                    }
                    break
        except Exception as e:  # noqa: BLE001
            logger.info("Apollo-blind sibling check failed for %s: %s", original, e)
    return out
