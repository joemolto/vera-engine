"""Website content extraction via Jina AI Reader and Claude analysis."""

import hashlib
import logging
import re

import httpx
from supabase import Client

from app.prompts.enrichment import (
    ENRICHMENT_CYBER_GTM_PROMPT,
    ENRICHMENT_SECURITY_PROMPT,
    CONTEXTUAL_WEBSITE_PROMPT,
    _format_services_list,
)
from app.services.claude import call_claude
from app.services.enrichers.cache import get_cached, set_cache
from app.services.enrichers.tech_stack import guarded_get
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Pages to scrape per domain. Organised by signal class so we can reason
# about coverage. 404s on individual paths are expected and silent —
# we just skip pages that don't exist on a given site.
#
# Tighter naming conventions vary widely by company, so each "topic" has
# multiple URL variants. Don't add a path that's behind heavy JS — Jina
# can't render those reliably.

BASE_PATHS = [
    "/",
    "/about", "/about-us",
    "/team", "/our-team", "/leadership",
    "/careers", "/jobs",
    "/blog",
    "/partners",
    "/privacy-policy",
]

# Customer / case-study / social-proof signals — tells us who they sell
# to (= our prospect's ICP), which downstream informs the "approach
# angle" without us guessing.
CUSTOMER_PATHS = [
    "/case-studies", "/case-study", "/cases",
    "/customers", "/our-customers",
    "/clients", "/our-clients",
    "/success-stories", "/stories",
    "/testimonials",
    "/resources/case-studies",
]

# Security / trust / compliance signals — for cyber prospects this
# establishes their public security posture: SOC 2, ISO 27001, PCI-DSS,
# HIPAA, GDPR, responsible disclosure, named DPO, named security
# leadership. Critical for MSSP / GRC positioning.
#
# "Trust center" is increasingly the canonical name for this; old sites
# use /security or /compliance directly.
SECURITY_PATHS = [
    "/security", "/security-overview", "/security/overview",
    "/trust", "/trust-center", "/trust-centre",
    "/compliance", "/certifications",
    "/legal/security", "/legal/compliance",
    "/privacy-security", "/data-protection", "/data-security",
    # Specific framework pages — cheap to try, high signal if hit
    "/soc2", "/soc-2",
    "/iso27001", "/iso-27001",
    "/gdpr", "/hipaa", "/pci-dss",
]

# Combined fetch list. Security + customer paths are now scanned for
# every prospect (used to be gated behind enrichment_mode); the cost is
# low (Jina silently 404s) and the signal is high — especially in cyber.
ALL_PATHS = BASE_PATHS + CUSTOMER_PATHS + SECURITY_PATHS

MAX_CONTENT_LENGTH = 30_000  # ~8-9 pages at PAGE_CHAR_CAP; 20K only fit 6

# Cap on sitemap-derived candidate paths. A 200-URL sitemap with every
# blog post + case study would blow our context budget; the keyword
# heuristic surfaces the high-value pages, this caps the total.
SITEMAP_MAX_PAGES = 20

# Path keywords that indicate a high-signal page even when the URL doesn't
# match ALL_PATHS exactly. Matches site-specific naming conventions like
# `/chemist4u-case-study`, `/soc2-overview`, `/our-trust-centre`, etc.
HIGH_SIGNAL_KEYWORDS = (
    "case-study", "case_study", "casestudy",
    "customer-story", "success-story", "client-story",
    "security", "trust", "compliance",
    "soc2", "soc-2", "iso27001", "iso-27001",
    "gdpr", "hipaa", "pci-dss", "pcidss",
    "certif",  # certifications, certified
    "privacy", "data-protection",
)


def enrich_website(
    db: Client,
    domain: str,
    enrichment_mode: str = "cyber_company_gtm",
    client_context: Optional[Dict[str, object]] = None,
    lead_context: Optional[Dict[str, str]] = None,
) -> dict:
    """Fetch a company website via Jina AI Reader and analyse with Claude.

    Args:
        db: Supabase client
        domain: Company domain (e.g. "cytix.io")
        enrichment_mode: "cyber_company_gtm" or "target_company_security"
        client_context: Optional dict with services_offered, client_positioning, client_name
        lead_context: Optional dict with lead_name and lead_title

    Returns:
        Enrichment result dict from Claude analysis
    """
    # Check cache first
    cached = get_cached(db, domain, "website", enrichment_mode)
    if cached:
        return cached

    # Path discovery strategy (in priority order):
    #
    # 1. /sitemap.xml — the authoritative list of pages the site actually
    #    publishes. Eliminates false 404s entirely because we only fetch
    #    paths the site itself confirms exist. We intersect the sitemap
    #    with ALL_PATHS so we still focus on the high-signal pages we
    #    care about (security, customers, case studies, about, etc.) and
    #    don't drown in 200 individual blog posts.
    #
    # 2. Hand-curated ALL_PATHS + probe-baseline 404 detection — fallback
    #    for sites without a sitemap (rare). Probe a guaranteed-404 URL
    #    once to establish the site's SPA fallback fingerprint, then
    #    reject anything matching it.
    fetched_pages: List[tuple] = []  # (path-or-url label, content)
    page_paths_hit: List[str] = []
    base_url = f"https://{domain}"

    sitemap_paths = _discover_paths_from_sitemap(domain)

    if sitemap_paths:
        # Sitemap available: union of three sources, all filtered by the
        # site's own published URL list:
        #
        # 1. Paths from ALL_PATHS confirmed by the sitemap. Locale-aware
        #    so /en/about and /about both match. Catches canonical pages.
        # 2. Sitemap URLs matching HIGH_SIGNAL_KEYWORDS — catches
        #    site-specific naming like /chemist4u-case-study or
        #    /soc2-overview.
        # 3. Shallow structural paths — depth ≤ 2 (locale-aware) and
        #    not inside a blog/news/resources subtree. Catches vendor
        #    service pages like /en/penetration-testing or /platform
        #    that don't match keywords.
        #
        # Capped at SITEMAP_MAX_PAGES with priority ranking so a sitemap
        # with hundreds of blog posts can't drown out the structural pages.
        # Sort sitemap_paths once so iteration order is deterministic and
        # English-prefixed paths (e.g. /en/about) come before /jp/, /fr/,
        # /de/ etc. — the locale dedup later wins by first-seen.
        sorted_sitemap = sorted(sitemap_paths)
        explicit = ["/"] + [
            p for p in sorted_sitemap
            if _strip_locale_prefix(p) in ALL_PATHS or p in ALL_PATHS
        ]
        keyword_hits = [
            p for p in sorted_sitemap
            if p not in explicit and _is_high_signal_path(p)
        ]
        shallow_hits = [
            p for p in sorted_sitemap
            if p not in explicit
            and p not in keyword_hits
            and _is_shallow_structural_path(p)
        ]
        # Rank within each bucket, then concatenate
        keyword_hits = sorted(keyword_hits, key=_path_priority)
        shallow_hits = sorted(shallow_hits, key=_path_priority)
        # Dedupe by locale-stripped path: if /en/about and /jp/about both
        # appear, keep only the first (English wins on alphabetical sort
        # against /jp/, /fr/, etc.). Prevents i18n sites from doubling
        # up on every page and burning the cap.
        candidate_paths = _dedupe_by_locale(
            list(dict.fromkeys(explicit + keyword_hits + shallow_hits))
        )[:SITEMAP_MAX_PAGES]
        logger.info(
            "Sitemap-driven scrape for %s: %d paths chosen (%d explicit, %d keyword, %d shallow; sitemap has %d total)",
            domain, len(candidate_paths), len(explicit), len(keyword_hits), len(shallow_hits), len(sitemap_paths),
        )
        probe_length = 0
        seen_content: set[str] = set()
    else:
        # No sitemap: fall back to scraping all ALL_PATHS with probe-based
        # SPA-shell rejection.
        candidate_paths = list(ALL_PATHS)
        probe_path = "/__vera_probe_does_not_exist_xyz123"
        probe_content = _fetch_page(f"{base_url}{probe_path}")  # probe — don't persist
        probe_fingerprint = (
            _content_fingerprint(probe_content, probe_path) if probe_content else None
        )
        probe_length = len(probe_content) if probe_content else 0
        seen_content = {probe_fingerprint} if probe_fingerprint else set()
        logger.info(
            "No sitemap for %s — falling back to ALL_PATHS scan (probe shell: %d chars)",
            domain, probe_length,
        )

    for path in candidate_paths:
        url = f"{base_url}{path}"
        content = _fetch_page(url, db=db, domain=domain)
        if not content:
            continue

        # Coarse first-pass: anything dramatically shorter than the
        # probe is unlikely to be a real page. Use probe length as the
        # baseline — keeps thin-but-real pages on sites with verbose
        # 404s, and aggressively filters chunky 404s on sites with
        # large fallback pages.
        if probe_length and len(content) < max(1500, probe_length + 200):
            # Not enough longer than the SPA shell to be real
            logger.debug(
                "Skipping %s — %d chars vs SPA 404 shell ~%d",
                path, len(content), probe_length,
            )
            continue
        elif not probe_length and len(content) < 1500:
            logger.debug("Skipping %s — too short (%d chars)", path, len(content))
            continue

        # Fingerprint dedupe — also catches /about and /about-us if they
        # both redirect to the same canonical URL. Path is stripped from
        # the content before hashing so SPA shells (which embed the
        # requested path in the title) all dedupe to one entry.
        fingerprint = _content_fingerprint(content, path)
        if fingerprint in seen_content:
            logger.debug("Skipping %s — content matches probe or another page", path)
            continue
        seen_content.add(fingerprint)

        fetched_pages.append((path, content))
        page_paths_hit.append(path)

    # Externally hosted trust centres (trust.<domain>, security.<domain>,
    # SafeBase/Vanta/Drata-style portals linked from the site) live off the
    # main domain, so the path scan above can never reach them.
    for label, content in _discover_external_trust_pages(domain, fetched_pages, db=db):
        fetched_pages.append((label, content))
        page_paths_hit.append(label)

    if not fetched_pages:
        return {"error": f"Could not fetch any pages from {domain}"}

    # Assemble with priority + per-page cap. The old flat join truncated at
    # MAX_CONTENT_LENGTH with security paths appended last, so on content-
    # heavy sites the /security page was fetched, listed in _pages_scraped,
    # and then silently cut before Claude ever saw it (how Monzo's trust
    # data went missing).
    combined = _assemble_page_content(fetched_pages)

    # Select prompt based on mode and client context
    if client_context and client_context.get("services_offered"):
        prompt = CONTEXTUAL_WEBSITE_PROMPT.format(
            client_name=client_context.get("client_name", "the client"),
            services_offered_list=_format_services_list(client_context.get("services_offered", [])),
            client_positioning=client_context.get("client_positioning", ""),
            domain=domain,
            page_content=combined,
            lead_name=lead_context.get("lead_name", "N/A") if lead_context else "N/A",
            lead_title=lead_context.get("lead_title", "N/A") if lead_context else "N/A",
        )
    elif enrichment_mode == "target_company_security":
        prompt = ENRICHMENT_SECURITY_PROMPT.format(domain=domain, page_content=combined)
    else:
        prompt = ENRICHMENT_CYBER_GTM_PROMPT.format(domain=domain, page_content=combined)

    # Call Claude
    result = call_claude(prompt)

    # Record which paths actually returned content — useful for the
    # dashboard ("Vera scraped 7 pages from this site") and for
    # debugging when a SOC2 page exists but Jina didn't render it.
    result["_pages_scraped"] = page_paths_hit

    # Attach raw content for downstream enrichers (not cached/stored)
    result["_raw_website_content"] = combined

    # Cache the result (without raw content)
    if "error" not in result:
        cache_result = {k: v for k, v in result.items() if not k.startswith("_")}
        set_cache(db, domain, "website", enrichment_mode, cache_result)

    return result


def _dedupe_by_locale(paths: List[str]) -> List[str]:
    """If both `/en/about` and `/jp/about` are in the list, keep only one
    per logical path (locale-stripped). Preserves order — first occurrence
    wins, so put English-prefixed paths first if you want them favoured.
    """
    seen_logical: set[str] = set()
    out: List[str] = []
    for p in paths:
        logical = _strip_locale_prefix(p)
        if logical in seen_logical:
            continue
        seen_logical.add(logical)
        out.append(p)
    return out


def _strip_locale_prefix(path: str) -> str:
    """Strip a 2-letter locale prefix if present (e.g. /en/about → /about,
    /jp/blog → /blog). Used to match i18n paths against ALL_PATHS."""
    parts = path.split("/")
    # parts looks like ['', 'en', 'about'] for '/en/about'
    if len(parts) >= 3 and len(parts[1]) == 2 and parts[1].isalpha():
        return "/" + "/".join(parts[2:])
    return path


def _is_high_signal_path(path: str) -> bool:
    """True if a path's text contains a keyword that suggests it's a
    case study, security page, or compliance page worth scraping —
    even if it doesn't exactly match anything in ALL_PATHS."""
    p = path.lower()
    return any(k in p for k in HIGH_SIGNAL_KEYWORDS)


# Path segments that mark a URL as content/post rather than a structural
# page. Used to exclude blog posts, news articles, and individual
# resources from the "shallow path" heuristic.
CONTENT_SEGMENTS = (
    "/blog/", "/posts/", "/post/", "/news/", "/article/", "/articles/",
    "/resources/", "/insights/", "/press/",
)


def _is_shallow_structural_path(path: str) -> bool:
    """True if the path looks like a top-level structural page (about,
    services, product, platform, etc.) — depth <= 2 segments AND not
    inside a blog/news/resources subtree.

    Catches the cyber-vendor service pages that don't match ALL_PATHS
    or HIGH_SIGNAL_KEYWORDS — e.g. /en/penetration-testing,
    /en/incident-response, /en/red-teaming, /en/platform.
    """
    if any(seg in path for seg in CONTENT_SEGMENTS):
        return False
    # Strip locale prefix for depth counting so /en/about and /about
    # both count as "depth 1"
    normalised = _strip_locale_prefix(path)
    # depth = number of slashes after the leading one
    depth = normalised.count("/")
    return 1 <= depth <= 2


def _path_priority(path: str) -> tuple:
    """Sort key for sitemap candidate paths. Lower tuples win.

    Priorities (in order):
    1. Case studies / customer stories (highest — reveal proven ICP)
    2. Security / trust / compliance / framework pages
    3. Other keyword matches (privacy, data-protection, certifications)
    4. Path depth — shallower paths preferred over deep ones (a /security
       page beats a /resources/blog-post-about-security article)
    """
    p = path.lower()
    if any(k in p for k in ("case-study", "case_study", "casestudy",
                              "customer-story", "success-story", "client-story")):
        bucket = 0
    elif any(k in p for k in ("security", "trust", "compliance",
                                "soc2", "soc-2", "iso27001", "iso-27001",
                                "gdpr", "hipaa", "pci-dss", "pcidss")):
        bucket = 1
    elif any(k in p for k in ("certif",)):
        bucket = 2
    else:
        bucket = 3
    depth = path.count("/")
    return (bucket, depth, path)


# Per-page slice of the combined-content budget. Caps any single verbose
# page (usually the homepage or blog) so it can't eat the whole
# MAX_CONTENT_LENGTH budget on its own.
PAGE_CHAR_CAP = 3_500

_SECURITY_PRIORITY_KEYWORDS = (
    "security", "trust", "compliance", "soc2", "soc-2",
    "iso27001", "iso-27001", "gdpr", "hipaa", "pci-dss", "pcidss",
    "certif", "data-protection",
)


def _assembly_priority(label: str) -> int:
    """Order pages for the combined prompt payload: homepage first (who
    they are), then security/trust pages (the class the flat join used to
    starve), then customer proof, then structural pages (about, careers),
    then blog/news posts. A blog post about security is bucket 4, not 1 —
    otherwise a security vendor's blog crowds out /about and /careers."""
    p = label.lower()
    if p == "/":
        return 0
    if "external trust centre" in p:
        return 1
    if any(seg in p for seg in CONTENT_SEGMENTS):
        return 4
    if (any(k in p for k in _SECURITY_PRIORITY_KEYWORDS)
            and _strip_locale_prefix(p).count("/") <= 2):
        return 1
    if any(k in p for k in ("case-stud", "case_stud", "casestud",
                              "customer", "client", "success-stor",
                              "testimonial")):
        return 2
    return 3


def _assemble_page_content(pages: List[tuple]) -> str:
    """Join fetched (label, content) pages into the prompt payload:
    priority order, each page capped at PAGE_CHAR_CAP, total capped at
    MAX_CONTENT_LENGTH. Ties within a priority bucket keep fetch order."""
    ordered = sorted(
        enumerate(pages),
        key=lambda t: (_assembly_priority(t[1][0]), t[0]),
    )
    blocks: List[str] = []
    total = 0
    dropped: List[str] = []
    for _, (label, content) in ordered:
        if total >= MAX_CONTENT_LENGTH:
            dropped.append(label)
            continue
        block = f"--- Page: {label} ---\n{content[:PAGE_CHAR_CAP]}"
        block = block[: MAX_CONTENT_LENGTH - total]
        blocks.append(block)
        total += len(block) + 2
    if dropped:
        logger.info(
            "Content budget hit — dropped %d low-priority pages: %s",
            len(dropped), ", ".join(dropped[:10]),
        )
    return "\n\n".join(blocks)


# Hosted trust-centre platforms, matched against links in already-fetched
# pages (footers usually carry them). Patterns are portal-shaped on
# purpose — a marketing mention of vanta.com is not a trust centre.
_TRUST_PORTAL_RE = re.compile(
    r"https?://(?:"
    r"[\w-]+\.safebase\.(?:io|us)"            # <company>.safebase.io
    r"|trust\.vanta\.com/[\w-]+"              # Vanta-hosted trust centres
    r"|(?:app|trust)\.drata\.com/trust[\w/-]*"
    r"|app\.conveyor\.com/profile/[\w-]+"
    r"|access\.whistic\.com/\S+"
    r"|trust\.oneleet\.com/\S+"
    r"|[\w-]+\.trustcloud\.ai"
    r")[^\s)\"'<>]*",
    re.IGNORECASE,
)

# Markdown link whose anchor text says "trust center/centre" — catches
# custom-domain portals whose host we can't know in advance.
_TRUST_ANCHOR_RE = re.compile(
    r"\[[^\]]*trust\s?cent(?:er|re)[^\]]*\]\((https?://[^)\s]+)\)",
    re.IGNORECASE,
)


def _discover_external_trust_pages(
    domain: str,
    fetched_pages: List[tuple],
    db: Optional[Client] = None,
) -> List[tuple]:
    """Find an externally hosted trust centre and fetch it.

    Two routes: (1) probe trust.<domain> and security.<domain> — the
    common custom-domain setup for SafeBase/Drata/Conveyor portals;
    (2) scan the pages already fetched for links to known trust platforms
    or links labelled "trust centre". Returns at most one (label,
    content) pair — one portal is plenty for the posture flags.
    """
    root = domain.lower()
    if root.startswith("www."):
        root = root[4:]
    candidates: List[str] = [f"https://trust.{root}", f"https://security.{root}"]

    corpus = "\n".join(content for _, content in fetched_pages)
    candidates += _TRUST_PORTAL_RE.findall(corpus)
    candidates += _TRUST_ANCHOR_RE.findall(corpus)

    seen: set[str] = set()
    for url in candidates:
        url = url.rstrip(".,;")
        if url in seen:
            continue
        seen.add(url)
        try:
            from urllib.parse import urlparse
            host = (urlparse(url).netloc or "").lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:
            continue
        # The main domain is already covered by the path scan — only
        # subdomains and third-party hosts count as "external".
        if host == root:
            continue
        final = _probe_trust_url(url, root)
        if not final:
            continue
        content = _fetch_page(final, db=db, domain=domain)
        if not content or len(content) < 500:
            continue
        logger.info("External trust centre found for %s: %s", domain, final)
        return [(f"{final} (external trust centre)", content)]
    return []


def _probe_trust_url(url: str, root: str) -> Optional[str]:
    """Cheap direct GET before spending a Jina fetch. Returns the final
    URL if the page looks real, None otherwise. A redirect back to the
    main site's homepage (the usual wildcard-DNS behaviour) is a miss."""
    try:
        with httpx.Client(timeout=6, follow_redirects=True) as client:
            resp = guarded_get(
                client, url,
                headers={"User-Agent": "Mozilla/5.0 (Vera/1.0)"},
            )
            if resp is None or resp.status_code >= 400 or len(resp.text) < 500:
                return None
            host = (resp.url.host or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host == root and resp.url.path in ("", "/"):
                return None
            return str(resp.url)
    except Exception:
        return None


def _discover_paths_from_sitemap(domain: str) -> Optional[set[str]]:
    """Read the site's sitemap(s) and return the set of paths it publishes.

    Tries `/sitemap.xml` first, then `/sitemap_index.xml`. If the file is
    a sitemap index (points to multiple sitemap files), recurses one
    level deep to collect URLs from each child. Returns None if no
    sitemap is found — caller falls back to ALL_PATHS + probe baseline.

    Hits the site directly (not via Jina) — sitemaps are XML and tiny.
    """
    candidates = [
        f"https://{domain}/sitemap.xml",
        f"https://{domain}/sitemap_index.xml",
        f"https://{domain}/sitemap-index.xml",
    ]
    paths: set[str] = set()

    try:
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            # robots.txt is the authoritative pointer — Monzo, for example,
            # declares `Sitemap: https://monzo.com/sitemap-index.xml` while
            # serving HTML at /sitemap.xml. Robots-declared URLs go first.
            robots = guarded_get(
                client, f"https://{domain}/robots.txt",
                headers={"User-Agent": "Mozilla/5.0 (Vera/1.0)"},
            )
            if robots is not None and robots.status_code == 200:
                import re as _re
                declared = _re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots.text)
                candidates = [
                    u for u in declared
                    if _url_to_path(u, domain) is not None or domain in u
                ] + candidates

            for url in candidates:
                resp = guarded_get(client, url, headers={"User-Agent": "Mozilla/5.0 (Vera/1.0)"})
                if resp is None:
                    continue
                if resp.status_code != 200 or "xml" not in resp.headers.get("content-type", "").lower():
                    continue

                # Parse without DOCTYPE-handling drama. ET handles namespaces
                # via the {ns}tag form; we just look for any <loc> regardless
                # of namespace.
                try:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(resp.text)
                except ET.ParseError:
                    continue

                # Detect sitemap index vs urlset
                tag = root.tag.rsplit("}", 1)[-1]
                if tag == "sitemapindex":
                    # Recurse one level — fetch each child sitemap
                    for sm in root.iter():
                        if sm.tag.rsplit("}", 1)[-1] == "loc" and sm.text:
                            paths.update(_extract_paths_from_sitemap_url(client, sm.text.strip(), domain))
                else:
                    # urlset — extract <loc> directly
                    for loc in root.iter():
                        if loc.tag.rsplit("}", 1)[-1] == "loc" and loc.text:
                            p = _url_to_path(loc.text.strip(), domain)
                            if p:
                                paths.add(p)

                if paths:
                    return paths
    except Exception as e:
        logger.debug("Sitemap discovery failed for %s: %s", domain, e)

    return None


def _extract_paths_from_sitemap_url(client: httpx.Client, url: str, domain: str) -> set[str]:
    """Fetch a single sitemap file (referenced from a sitemap index) and
    return the paths it publishes."""
    paths: set[str] = set()
    try:
        resp = guarded_get(client, url, headers={"User-Agent": "Mozilla/5.0 (Vera/1.0)"})
        if resp is None or resp.status_code != 200:
            return paths
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        for loc in root.iter():
            if loc.tag.rsplit("}", 1)[-1] == "loc" and loc.text:
                p = _url_to_path(loc.text.strip(), domain)
                if p:
                    paths.add(p)
    except Exception:
        pass
    return paths


def _url_to_path(url: str, domain: str) -> Optional[str]:
    """Convert a full URL to a path, but only if it's on the same domain
    (or a subdomain) as the prospect. Cross-domain URLs in a sitemap
    (CDN URLs, schema.org references) are filtered out.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower().lstrip("www.")
        if host != domain.lower().lstrip("www.") and not host.endswith(f".{domain.lower()}"):
            return None
        path = parsed.path or "/"
        # Strip trailing slash to match our ALL_PATHS shape (which uses
        # bare paths, no trailing slash except for the root)
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        return path
    except Exception:
        return None


def _content_fingerprint(content: str, path: str) -> str:
    """Hash content with path-specific text normalised out.

    SPA frameworks render the requested path into their 404 fallback
    (e.g. "Page not found — /soc2" vs "/iso27001"), so the raw bytes of
    every bogus path differ slightly even though the body is identical.
    Strip every occurrence of the path (with and without leading slash)
    before hashing so all those shells fingerprint identically.
    """
    normalised = (
        content.replace(path, "<PATH>").replace(path.lstrip("/"), "<PATH>")
    )
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def _fetch_page(url: str, db: Optional[Client] = None, domain: Optional[str] = None) -> Optional[str]:
    """Fetch a page using Jina AI Reader for clean content extraction.

    When `db` and `domain` are provided, the raw markdown payload is
    persisted to the raw_artifacts corpus for future re-extraction.
    """
    jina_url = f"https://r.jina.ai/{url}"
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            response = client.get(
                jina_url,
                headers={
                    "Accept": "text/plain",
                    "X-Timeout": "10",
                },
            )
            response.raise_for_status()
            text = response.text.strip()
            if text and len(text) > 50:
                if db is not None and domain:
                    try:
                        from app.services.raw_artifacts import persist_raw
                        persist_raw(
                            db, domain, "jina", text,
                            source_endpoint=url,
                        )
                    except Exception:
                        pass
                return text
            return None
    except Exception as e:
        logger.debug("Jina AI could not fetch %s: %s", url, e)
        return None
