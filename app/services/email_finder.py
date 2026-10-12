"""Email resolution waterfall — always returns an email + confidence score.

Layers (in order):
  0. Apollo email          → 80% (Apollo marks most of its own emails 'unverified')
  1. Findymail by LinkedIn → 90% (verified) / 70% (unverified)
  2. Findymail by name     → 90% (verified) / 70% (unverified)
  3. Permutation guess     → 35% confidence (best common B2B pattern)
  4. AI guess (Claude)     → 10% confidence

Every chosen address then runs through an accuracy pass (_finalise):
dead-domain DNS check (free), account-domain mismatch flag, and a bounded
Findymail verify that raises the confidence when the address is deliverable
(apollo→99, permutation→85, …) or collapses it when it would bounce.
A bouncing candidate doesn't end the search — the waterfall falls through
to the next layer and the bounced address is only returned (≤15) when
every later layer also fails.

Every lead always gets an email. The rep sees the confidence score and
decides whether to use it.
"""

import logging
from typing import Any, Dict, List, Optional

import dns.resolver
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

FINDYMAIL_BASE = "https://app.findymail.com/api"
FINDYMAIL_TIMEOUT = 15  # seconds per request

# Common B2B email patterns, ordered by prevalence.
# {f} = first name, {l} = last name, {fi} = first initial, {li} = last initial
_EMAIL_PATTERNS = [
    "{f}.{l}",      # first.last@domain.com  (most common)
    "{f}{l}",        # firstlast@domain.com
    "{fi}{l}",       # flast@domain.com
    "{f}",           # first@domain.com
    "{f}_{l}",       # first_last@domain.com
    "{fi}.{l}",      # f.last@domain.com
    "{f}{li}",       # firstl@domain.com
    "{l}.{f}",       # last.first@domain.com
    "{l}",           # last@domain.com
    "{fi}{li}",      # fl@domain.com
]

# Discovery stamps Apollo-search rows that ship no address with a placeholder
# (see discovery._person_to_lead_input). The waterfall must treat it as "no
# email" — trusting it as a real Apollo address gave placeholder rows 95%
# confidence and the real resolution never ran.
PLACEHOLDER_EMAIL_SUFFIX = "@unresolved.local"

# Confidence when Findymail's verifier confirms deliverability, per source.
# A verified permutation guess is a real address; a verified Apollo email is
# as good as it gets.
_VERIFIED_CONFIDENCE = {
    "apollo": 99,
    "findymail_linkedin": 90,
    "findymail_name": 90,
    "permutation": 85,
    "ai_guess": 80,
}

_DNS_TIMEOUT = 3.0


def domain_has_mail_dns(domain: str) -> Optional[bool]:
    """Can this domain receive mail at all, per DNS?

    True = MX (or the RFC 5321 A/AAAA fallback) exists; False = DNS says
    nothing can receive mail there (certain hard bounce); None = couldn't
    check (resolver unreachable/timeout) — fail-open, never block on None.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return None
    resolver = dns.resolver.Resolver()
    resolver.timeout = resolver.lifetime = _DNS_TIMEOUT
    try:
        if resolver.resolve(domain, "MX"):
            return True
    except dns.resolver.NXDOMAIN:
        return False
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        pass
    except Exception:  # noqa: BLE001 — timeouts, broken local resolver
        return None
    # No MX record — mail falls back to the A/AAAA host (RFC 5321 §5.1).
    for rtype in ("A", "AAAA"):
        try:
            if resolver.resolve(domain, rtype):
                return True
        except dns.resolver.NXDOMAIN:
            return False
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            continue
        except Exception:  # noqa: BLE001
            return None
    return False


def mail_domains_match(email_domain: str, company_domain: str) -> bool:
    """Same mail estate: exact match or one is a subdomain of the other
    (us.gt.com ↔ gt.com counts). Different apexes do NOT match — that's the
    legacy/pre-merger-domain trap (a contact at one company with an
    address on a former parent's domain, 2026-08-25). Empty inputs → True (nothing to
    compare, don't flag)."""
    e = (email_domain or "").strip().lower().lstrip("@")
    c = (company_domain or "").strip().lower()
    if c.startswith("www."):
        c = c[4:]
    if not e or not c:
        return True
    return e == c or e.endswith("." + c) or c.endswith("." + e)


def infer_email_pattern(email: str, first_name: str, last_name: str) -> Optional[str]:
    """Which known pattern produced this address for this person?
    Returns the pattern template ('{f}.{l}' etc.) or None if none match."""
    email = (email or "").strip().lower()
    f = (first_name or "").strip().lower()
    l = (last_name or "").strip().lower()
    if "@" not in email or not f or not l:
        return None
    local = email.split("@", 1)[0]
    fi, li = f[0], l[0]
    for pattern in _EMAIL_PATTERNS:
        if pattern.format(f=f, l=l, fi=fi, li=li) == local:
            return pattern
    return None


def check_pattern_consistency(
    email: str, first_name: str, last_name: str, company_domain: str
) -> Optional[bool]:
    """Does this address fit the company's verified email convention?

    2026-08-28: a verified jsmith@example.com establishes
    first-initial+surname; an address in another shape doesn't fit it and
    hard-bounced. For domains that block SMTP probes, convention conflict
    is the best remaining signal.

    Returns True (fits the established pattern), False (conflicts with it),
    or None (no established pattern / no names / lookup failed — fail-open).
    Conservative: a pattern is only 'established' when verified sibling
    addresses at the exact domain all agree on one template.
    """
    email = (email or "").strip().lower()
    domain = (company_domain or "").strip().lower()
    if "@" not in email or not domain or not first_name or not last_name:
        return None
    mail_domain = email.split("@", 1)[1]

    def _sibling_patterns(q_extra):
        try:
            from app.database import get_supabase
            q = (
                get_supabase().table("leads")
                .select("email, first_name, last_name")
                .ilike("email", f"%@{mail_domain}")
                .neq("email", email)
                .limit(10)
            )
            siblings = q_extra(q).execute().data or []
        except Exception as e:  # noqa: BLE001
            logger.info("pattern-consistency lookup failed for %s: %s", mail_domain, e)
            return None
        return {
            p for p in (
                infer_email_pattern(s.get("email") or "", s.get("first_name") or "", s.get("last_name") or "")
                for s in siblings
            ) if p
        }

    # Verified siblings are the strongest basis. Probe-blocking domains
    # never have any, so fall back to Apollo-SOURCED
    # siblings — real addresses from the vendor, not our guesses — still
    # requiring unanimity before anything counts as 'the convention'.
    patterns = _sibling_patterns(lambda q: q.eq("email_verified", True))
    if patterns is None:
        return None
    if not patterns:
        patterns = _sibling_patterns(
            lambda q: q.eq("email_source", "apollo").gte("email_confidence", 70)
        )
        if patterns is None:
            return None
    if len(patterns) != 1:
        return None  # no siblings, or the company mixes conventions
    established = next(iter(patterns))
    f = first_name.strip().lower()
    l = last_name.strip().lower()
    expected = established.format(f=f, l=l, fi=f[0], li=l[0])
    return email.split("@", 1)[0] == expected


def _finalise(
    result: Dict[str, Any],
    company_domain: str,
    first_name: str = "",
    last_name: str = "",
) -> Dict[str, Any]:
    """Accuracy pass on the waterfall's chosen address.

    1. Dead-domain DNS check (free) — no MX/A means certain bounce, so the
       confidence collapses to 5 and no verify credit is spent.
    2. Account-domain mismatch flag — the address may still work, but at
       outbound volume it's the classic sender-reputation burn.
    3. Findymail verify (bounded: ≤3 calls, permutation runners-up only) —
       skipped when Findymail's finder already returned it verified.
    4. Pattern-consistency check for UNVERIFIABLE addresses only: an
       address that conflicts with the company's verified convention is
       capped at 40 (the pattern-conflict signal for probe-blocking domains).
    Fail-open throughout: DNS/verifier outages leave the layer confidence
    untouched.
    """
    email = result.get("email") or ""
    if "@" not in email:
        return result
    checks: Dict[str, Any] = {}
    email_domain = email.rsplit("@", 1)[1].lower()

    if not mail_domains_match(email_domain, company_domain):
        checks["domain_mismatch"] = True

    if domain_has_mail_dns(email_domain) is False:
        checks["dead_domain"] = True
        result["email_verified"] = False
        result["confidence"] = min(result["confidence"], 5)
        result["checks"] = checks
        return result

    email_verified = result.get("email_verified")  # True when Findymail's finder pre-verified
    if email_verified is not True:
        email_verified = verify_email(email)
        if email_verified is True:
            result["confidence"] = _VERIFIED_CONFIDENCE.get(
                result.get("source") or "", result["confidence"]
            )
        elif email_verified is False:
            # The chosen guess bounces — permutation runners-up are free to
            # try, and one verified hit beats any unverified pattern.
            for guess in (result.get("all_guesses") or [])[1:3]:
                if verify_email(guess) is True:
                    result["email"] = guess
                    result["confidence"] = _VERIFIED_CONFIDENCE["permutation"]
                    email_verified = True
                    break
            else:
                result["confidence"] = min(result["confidence"], 15)

    if checks.get("domain_mismatch") and email_verified is not True:
        result["confidence"] = min(result["confidence"], 55)

    if email_verified is None:
        consistent = check_pattern_consistency(
            result.get("email") or "", first_name, last_name, company_domain
        )
        if consistent is False:
            checks["pattern_conflict"] = True
            result["confidence"] = min(result["confidence"], 40)

    result["email_verified"] = email_verified
    if checks:
        result["checks"] = checks
    return result


def verify_email(email: str) -> Optional[bool]:
    """SMTP-level verification via Findymail /verify.

    Returns True (deliverable), False (undeliverable), or None (unknown —
    no key, API error, or catch-all the verifier can't judge). Fail-open:
    None must never block a reveal; it just means "we couldn't check".
    Added 2026-08-19 after a customer's hard bounce — the waterfall's
    permutation/AI tiers were guesses with no post-hoc check.
    """
    email = (email or "").strip()
    if not email or "@" not in email or not settings.FINDYMAIL_API_KEY:
        return None
    try:
        with httpx.Client(timeout=FINDYMAIL_TIMEOUT) as client:
            res = client.post(
                f"{FINDYMAIL_BASE}/verify",
                json={"email": email},
                headers=_findymail_headers(),
            )
            res.raise_for_status()
            data = res.json() or {}
    except Exception as e:  # noqa: BLE001
        logger.info("Findymail verify failed for %s: %s", email, e)
        return None
    verified = data.get("verified")
    if isinstance(verified, bool):
        return verified
    return None


def find_email(
    *,
    apollo_email: str = "",
    linkedin_url: str = "",
    first_name: str = "",
    last_name: str = "",
    company_domain: str = "",
    company_name: str = "",
) -> Dict[str, Any]:
    """Run the email resolution waterfall. Always returns an email + confidence.

    Returns:
        {
            "email": "found@example.com",
            "confidence": 99,
            "source": "apollo" | "findymail_linkedin" | "findymail_name" | "permutation" | "ai_guess",
            "email_verified": True | False | None,  # Findymail verdict; None = couldn't check
            "checks": {"domain_mismatch": True, "dead_domain": True},  # only flags that fired
            "all_guesses": [...],  # only for permutation/ai layers
        }
    """
    # Defensive: strip any UTM params or paths from the domain
    if company_domain:
        company_domain = company_domain.split("?")[0].split("#")[0].split("/")[0].strip()

    # Discovery's no-address placeholder is not an Apollo email.
    apollo_email = (apollo_email or "").strip()
    if apollo_email.lower().endswith(PLACEHOLDER_EMAIL_SUFFIX):
        apollo_email = ""

    # A layer whose address the verifier says hard-bounces does NOT win —
    # the waterfall keeps hunting (2026-08: an Apollo address bounced
    # and we shipped it at 15 without ever asking Findymail). The first
    # bounced candidate is kept and only returned when every later layer
    # also fails, so callers still see the honest ≤15-confidence truth.
    bounced: Optional[Dict[str, Any]] = None

    def _settle(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nonlocal bounced
        finalised = _finalise(candidate, company_domain, first_name, last_name)
        if finalised.get("email_verified") is False:
            if bounced is None:
                bounced = finalised
            return None
        return finalised

    # Layer 0: Apollo's address. 80, not 95 — Apollo itself marks most of
    # these email_status='unverified'; the _finalise verify pass promotes a
    # deliverable one to 99 and collapses a bouncer.
    if apollo_email:
        settled = _settle(
            {
                "email": apollo_email,
                "confidence": 80,
                "source": "apollo",
            }
        )
        if settled:
            return settled

    # Layer 1: Findymail — find by LinkedIn URL
    if settings.FINDYMAIL_API_KEY and linkedin_url:
        result = _findymail_by_linkedin(linkedin_url)
        if result and result.get("email"):
            verified = result.get("verified")
            confidence = 90 if verified else 70
            logger.info("Findymail found email via LinkedIn URL for %s %s (verified=%s)",
                        first_name, last_name, verified)
            settled = _settle(
                {
                    "email": result["email"],
                    "confidence": confidence,
                    "source": "findymail_linkedin",
                    # Finder-verified addresses skip the redundant /verify call
                    **({"email_verified": True} if verified else {}),
                }
            )
            if settled:
                return settled

    # Layer 2: Findymail — find by name + domain
    name = f"{first_name} {last_name}".strip()
    if settings.FINDYMAIL_API_KEY and name and company_domain:
        result = _findymail_by_name(name, company_domain)
        if result and result.get("email"):
            verified = result.get("verified")
            confidence = 90 if verified else 70
            logger.info("Findymail found email via name+domain for %s at %s (verified=%s)",
                        name, company_domain, verified)
            settled = _settle(
                {
                    "email": result["email"],
                    "confidence": confidence,
                    "source": "findymail_name",
                    **({"email_verified": True} if verified else {}),
                }
            )
            if settled:
                return settled

    # Layer 3: Permutation guess — generate common patterns from name + domain
    if first_name and last_name and company_domain:
        guesses = _generate_permutations(first_name, last_name, company_domain)
        if guesses:
            best = guesses[0]
            logger.info("Email permutation guess for %s %s: %s (35%% confidence)",
                        first_name, last_name, best)
            settled = _settle(
                {
                    "email": best,
                    "confidence": 35,
                    "source": "permutation",
                    "all_guesses": guesses[:5],
                }
            )
            if settled:
                return settled

    # Layer 4: AI guess — Claude infers from whatever context we have
    email_guess = _ai_guess_email(first_name, last_name, company_name, company_domain)
    if email_guess:
        logger.info("AI email guess for %s %s: %s (10%% confidence)",
                     first_name, last_name, email_guess)
        settled = _settle(
            {
                "email": email_guess,
                "confidence": 10,
                "source": "ai_guess",
            }
        )
        if settled:
            return settled

    # Absolute last resort — construct the most basic possible guess
    if first_name and last_name and company_domain and bounced is None:
        basic = f"{first_name.lower()}.{last_name.lower()}@{company_domain}"
        settled = _settle(
            {
                "email": basic,
                "confidence": 10,
                "source": "permutation",
                "all_guesses": [basic],
            }
        )
        if settled:
            return settled

    # Every layer bounced or came up empty — the bounced candidate (from
    # the most trusted layer that produced one) is still the honest answer.
    if bounced:
        return bounced

    # Truly nothing to work with (no name or no domain)
    return {
        "email": None,
        "confidence": 0,
        "source": None,
    }


def _generate_permutations(first_name: str, last_name: str, domain: str) -> List[str]:
    """Generate common B2B email patterns for a given name + domain."""
    f = first_name.lower().strip()
    l = last_name.lower().strip()

    if not f or not l or not domain:
        return []

    fi = f[0]  # first initial
    li = l[0]  # last initial

    emails = []
    for pattern in _EMAIL_PATTERNS:
        local = pattern.format(f=f, l=l, fi=fi, li=li)
        emails.append(f"{local}@{domain}")

    return emails


def _ai_guess_email(
    first_name: str, last_name: str, company_name: str, company_domain: str
) -> Optional[str]:
    """Layer 4: Ask Claude to guess the most likely email address.

    Only called when Apollo, Findymail, and permutation all failed.
    This is generating new content (an email guess), not cross-checking —
    so AI is the right tool here.
    """
    if not first_name or not last_name:
        return None
    if not company_name and not company_domain:
        return None

    try:
        from app.services.claude import call_claude

        domain_hint = f"Company domain: {company_domain}" if company_domain else "Company domain: unknown"

        prompt = f"""Given this person's details, guess their most likely work email address.

Name: {first_name} {last_name}
Company: {company_name or 'unknown'}
{domain_hint}

Rules:
- If you know the domain, use the most common B2B email pattern (usually first.last@domain)
- If you don't know the domain, try to infer it from the company name (e.g. "Acme Corp" → acme.com)
- Return ONLY the email address, nothing else. No explanation, no quotes, just the email.
- If you truly cannot guess, return "NONE"
"""

        result = call_claude(prompt)
        # call_claude returns a dict — the raw text might be in different places
        # depending on whether it parsed as JSON or not
        if isinstance(result, dict):
            if result.get("error"):
                return None
            # If Claude returned valid JSON with an email key
            email = result.get("email") or ""
            if email and "@" in email and email != "NONE":
                return email.strip().lower()
            # Try raw text extraction
            raw = result.get("raw_text") or result.get("text") or ""
            if raw and "@" in raw and "NONE" not in raw:
                # Extract just the email from the response
                for word in raw.split():
                    word = word.strip().strip('"').strip("'").strip()
                    if "@" in word and "." in word.split("@")[-1]:
                        return word.lower()
        return None
    except Exception as e:
        logger.warning("AI email guess failed for %s %s: %s", first_name, last_name, e)
        return None


# ---- Findymail API helpers ----

def _findymail_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.FINDYMAIL_API_KEY}",
        "Content-Type": "application/json",
    }


def _findymail_by_linkedin(linkedin_url: str) -> Optional[Dict[str, Any]]:
    """Findymail Layer 1: find email from LinkedIn profile URL."""
    try:
        with httpx.Client(timeout=FINDYMAIL_TIMEOUT) as client:
            response = client.post(
                f"{FINDYMAIL_BASE}/search/linkedin",
                headers=_findymail_headers(),
                json={"linkedin_url": linkedin_url},
            )

            if response.status_code == 404:
                logger.info("Findymail LinkedIn endpoint returned 404 — email not found")
                return None

            if response.status_code == 422:
                logger.info("Findymail LinkedIn endpoint returned 422 — trying name+domain fallback")
                return None

            if not response.is_success:
                logger.warning("Findymail LinkedIn search HTTP %d: %s",
                               response.status_code, response.text[:300])
                return None

            data = response.json()
            contact = data.get("contact") or data
            email = contact.get("email") or ""
            if email:
                return {"email": email, "verified": contact.get("verified")}
            return None

    except httpx.TimeoutException:
        logger.warning("Findymail LinkedIn search timed out for %s", linkedin_url)
        return None
    except Exception as e:
        logger.warning("Findymail LinkedIn search error: %s", e)
        return None


def _findymail_by_name(name: str, domain: str) -> Optional[Dict[str, Any]]:
    """Findymail Layer 2: find email from name + company domain."""
    try:
        with httpx.Client(timeout=FINDYMAIL_TIMEOUT) as client:
            response = client.post(
                f"{FINDYMAIL_BASE}/search/name",
                headers=_findymail_headers(),
                json={"name": name, "domain": domain},
            )

            if response.status_code == 404:
                logger.info("Findymail name+domain returned 404 for %s at %s", name, domain)
                return None

            if not response.is_success:
                logger.warning("Findymail name+domain HTTP %d: %s",
                               response.status_code, response.text[:300])
                return None

            data = response.json()
            contact = data.get("contact") or data
            email = contact.get("email") or ""
            if email:
                return {"email": email, "verified": contact.get("verified")}
            return None

    except httpx.TimeoutException:
        logger.warning("Findymail name+domain timed out for %s at %s", name, domain)
        return None
    except Exception as e:
        logger.warning("Findymail name+domain error: %s", e)
        return None
