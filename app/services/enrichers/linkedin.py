"""LinkedIn content analysis enrichment."""

import logging
from typing import Any, Dict, List, Optional

from app.prompts.enrichment import LINKEDIN_CONTENT_PROMPT
from app.services.claude import call_claude

logger = logging.getLogger(__name__)


def enrich_linkedin_content(
    first_name: str = "",
    last_name: str = "",
    title: str = "",
    company_name: str = "",
    recent_posts: Optional[List[dict]] = None,
    client_context: Optional[Dict[str, Any]] = None,
) -> dict:
    """Analyse a prospect's LinkedIn posts for personalisation hooks + buying signals.

    If `client_context` is provided, the prompt is tailored to that client's
    product and buying triggers — so the analysis is framed in terms of
    "what would matter to THIS sales team" rather than a generic cybersecurity
    framing. This is what makes the personalisation hooks feel sharp.

    The result includes a `top_relevant_posts` array — Claude picks the 5
    posts most useful for outreach to {client_name}, and we hydrate each
    with the full post text, date, URL and engagement stats from the
    scraped posts (looked up by Claude's chosen indices). This gives the
    dashboard both the analysis AND the receipts.

    If no posts provided, returns a status indicating content wasn't checked.
    No caching — posts are ephemeral, provided per-request.
    """
    if not recent_posts:
        return {"status": "linkedin_content_not_checked"}

    # Format posts for the prompt — numbered so Claude can refer to them
    # by index, which lets us hydrate full post details from the original
    # scraped data without trusting Claude to echo the text back.
    posts_text = "\n\n".join(
        f"Post {i + 1} ({p.get('date', 'date unknown')}) — "
        f"{p.get('likes', 0)} likes, {p.get('comments', 0)} comments:\n"
        f"\"{p.get('text', '')}\""
        for i, p in enumerate(recent_posts)
    )

    # Pull client framing from context — fall back to a generic stance if absent.
    ctx = client_context or {}
    client_name = ctx.get("client_name") or ctx.get("company_name") or "the sales team"
    what_they_sell = ctx.get("what_they_sell") or "B2B services"

    ideal_triggers_raw = ctx.get("ideal_triggers")
    if isinstance(ideal_triggers_raw, list):
        ideal_triggers = "\n".join(f"- {t}" for t in ideal_triggers_raw if t)
    elif isinstance(ideal_triggers_raw, str) and ideal_triggers_raw.strip():
        ideal_triggers = ideal_triggers_raw
    else:
        ideal_triggers = "Any signal that the prospect is actively buying or growing."

    prompt = LINKEDIN_CONTENT_PROMPT.format(
        client_name=client_name,
        what_they_sell=what_they_sell,
        ideal_triggers=ideal_triggers,
        first_name=first_name,
        last_name=last_name,
        title=title,
        company_name=company_name,
        posts_text=posts_text,
    )

    result = call_claude(prompt)

    # Hydrate top_relevant_posts with the full post details from the scraped
    # data, looked up by Claude's chosen indices. We never trust Claude to
    # echo post text back — index lookups are deterministic and can't
    # hallucinate.
    if isinstance(result, dict):
        result["top_relevant_posts"] = _hydrate_top_posts(
            result.get("top_relevant_posts") or [],
            recent_posts,
        )
        # Always include total_posts_scraped so the UI can say "5 of 20 most relevant"
        result["total_posts_scraped"] = len(recent_posts)

    return result


def _hydrate_top_posts(
    top_picks: List[Any],
    scraped_posts: List[dict],
) -> List[Dict[str, Any]]:
    """Convert Claude's top_relevant_posts (index + why_it_matters) into full
    post records with text/date/url/engagement looked up from the scraped data.

    Defensive: if Claude returns garbage indices or shapes, we silently drop
    invalid entries rather than failing the whole enrichment.
    """
    hydrated: List[Dict[str, Any]] = []
    seen_indices: set = set()

    for pick in top_picks:
        if not isinstance(pick, dict):
            continue

        # Claude is asked to use 1-based indices (Post 1, Post 2, ...).
        # Convert to 0-based for the array lookup.
        raw_idx = pick.get("post_index")
        try:
            idx = int(raw_idx) - 1
        except (TypeError, ValueError):
            continue

        if idx < 0 or idx >= len(scraped_posts):
            continue
        if idx in seen_indices:
            continue
        seen_indices.add(idx)

        post = scraped_posts[idx]
        hydrated.append({
            "text": post.get("text", ""),
            "date": post.get("date", ""),
            "url": post.get("url", ""),
            "likes": post.get("likes", 0),
            "comments": post.get("comments", 0),
            "shares": post.get("shares", 0),
            "why_it_matters": (pick.get("why_it_matters") or "").strip(),
        })

        if len(hydrated) >= 5:
            break

    return hydrated
