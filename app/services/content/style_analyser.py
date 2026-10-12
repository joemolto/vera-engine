"""Style guide and voice profile generation from LinkedIn posts."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.prompts.content import STYLE_ANALYSIS_PROMPT, VOICE_ANALYSIS_PROMPT
from app.services.claude import call_claude

logger = logging.getLogger(__name__)


def analyse_style(posts: List[Dict], person_name: str, person_context: str) -> Dict[str, Any]:
    """Analyse LinkedIn posts and produce a structured style guide.

    Takes raw post data (text, date, likes, comments) and sends to Claude
    for style pattern extraction.
    """
    posts_text = _format_posts(posts)

    prompt = STYLE_ANALYSIS_PROMPT.format(
        person_name=person_name,
        person_context=person_context or "LinkedIn thought leader",
        posts_text=posts_text,
    )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Style analysis failed: %s", result.get("error"))
        return result

    logger.info(
        "Style analysis complete for %s — %d posts analysed",
        person_name, len(posts),
    )
    return result


def analyse_client_voice(
    posts: List[Dict], client_name: str, company_name: str
) -> Dict[str, Any]:
    """Extract the client's own voice patterns from their LinkedIn posts.

    Focused on vocabulary, topics, opinions, and natural communication style
    so generated content sounds like them.
    """
    posts_text = _format_posts(posts)

    prompt = VOICE_ANALYSIS_PROMPT.format(
        client_name=client_name,
        company_name=company_name,
        posts_text=posts_text,
    )

    result = call_claude(prompt)

    if "error" in result:
        logger.error("Voice analysis failed for %s: %s", client_name, result.get("error"))
        return result

    logger.info(
        "Voice analysis complete for %s at %s — %d posts analysed",
        client_name, company_name, len(posts),
    )
    return result


def _format_posts(posts: List[Dict]) -> str:
    """Format a list of post dicts into readable text for the prompt."""
    formatted = []
    for i, post in enumerate(posts, 1):
        lines = [f"### Post {i}"]
        lines.append(post.get("text", ""))
        meta_parts = []
        if post.get("date"):
            meta_parts.append(f"Date: {post['date']}")
        if post.get("likes") is not None:
            meta_parts.append(f"Likes: {post['likes']}")
        if post.get("comments") is not None:
            meta_parts.append(f"Comments: {post['comments']}")
        if meta_parts:
            lines.append(f"[{', '.join(meta_parts)}]")
        formatted.append("\n".join(lines))
    return "\n\n".join(formatted)
