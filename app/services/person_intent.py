"""Haiku 4.5 classifier for person-level LinkedIn activity.

Given one item from `linkedin_person.py` (a post written by the person,
or a comment/reaction they left on someone else's post), decide:

  - Is this signal-bearing for the rep?
  - If so, which person_* signal_type does it map to?

This is the strict noise filter. LinkedIn is 80% weekend mood, job
anniversaries, generic motivation, and dog photos. We want the 20%
that actually tells the rep something useful — what the buyer is
thinking about, who they're engaging with, what they're saying.

Cost: Haiku 4.5 at $1/$5 per 1M in/out. Per call ≈ $0.0002. A 15-item
scrape for one person ≈ $0.003. Negligible vs the Apify spend that
produced the items.

Fails open as 'drop': API failure or unparseable JSON → drop the item
rather than write an unclassified signal.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from app.config import settings
from anthropic import Anthropic

logger = logging.getLogger(__name__)


MODEL = "claude-haiku-4-5"
CONFIDENCE_FLOOR = 0.6
MAX_SNIPPET_CHARS = 1500


_CLIENT: Optional[Anthropic] = None


def _client() -> Optional[Anthropic]:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    key = settings.ANTHROPIC_API_KEY
    if not key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — person_intent classifier disabled"
        )
        return None
    _CLIENT = Anthropic(api_key=key)
    return _CLIENT


_SIGNAL_TYPES = (
    "person_posted_relevant_topic",
    "person_engaged_with_topic",
    "person_engaged_with_competitor",
)


def _system_prompt(*, vendor_pitch: str, competitor_list: List[str]) -> str:
    """Build the system prompt. Vendor pitch tells the classifier what
    "relevant topic" means for this client; competitor list flips the
    `engaged_with_competitor` rail."""
    competitors_block = (
        "\n".join(f"  - {c}" for c in competitor_list)
        if competitor_list
        else "  (none configured — never emit person_engaged_with_competitor)"
    )
    return f"""You are a sales-intelligence classifier for person-level LinkedIn activity.

A B2B rep monitors a set of target accounts. We've identified the key buyer at each account (CISO, Head of Security, etc.) and pulled their recent LinkedIn activity — both their own posts AND comments/reactions they've left on OTHER people's posts. Your job: decide whether THIS specific item gives the rep something useful to act on.

# The vendor
{vendor_pitch}

# The vendor's competitors (named in icp_config)
{competitors_block}

# Output

Respond with a single JSON object — no prose, no markdown, no code fences:

{{
  "is_signal_bearing": <true | false>,
  "signal_type": "<one of person_posted_relevant_topic | person_engaged_with_topic | person_engaged_with_competitor | none>",
  "confidence": <0.0–1.0>,
  "quoted_excerpt": "<verbatim sentence/fragment that captures the signal, ≤280 chars, or empty>",
  "summary": "<your own one-sentence summary of why the rep would care, ≤200 chars, or empty>"
}}

# Signal types — pick exactly one (or "none")

**person_posted_relevant_topic** — they POSTED on their own profile about a topic relevant to the vendor's pitch. Real opinions, observations, problems they're facing, security commentary, hiring announcements they made, product/vendor evaluations. The buyer is broadcasting that this topic is on their mind.

**person_engaged_with_topic** — they COMMENTED OR REACTED on someone else's post on a relevant topic. Listening rather than broadcasting, but still revealing interest. Comments are stronger signal than reactions.

**person_engaged_with_competitor** — they COMMENTED OR REACTED on a post from one of the named competitors (or about one of them by name). This is the strongest single signal — they're actively engaging with the vendor we're trying to displace.

# Drop (return is_signal_bearing=false, signal_type="none")

- Weekend mood, holiday wishes, generic motivation, gratitude posts
- Job anniversaries ("celebrating 5 years at...")
- Out-of-office, conference selfies without context
- Generic congratulations to other people
- Vague leadership/hustle quotes without specific topic
- Anything where the topic is clearly unrelated to the vendor pitch

# Be strict

When in doubt, drop. Better to surface 1 strong signal per person per week than 10 vague items. Confidence below {CONFIDENCE_FLOOR} = drop."""


def classify_item(
    item: Dict[str, Any],
    *,
    vendor_pitch: str,
    competitor_list: List[str],
) -> Optional[Dict[str, Any]]:
    """Classify one activity item from linkedin_person. Returns a
    decision dict if signal_bearing, else None.

    The decision shape (for the caller to use as signal_data):
      {
        "signal_type": "...",
        "confidence": 0.85,
        "quoted_excerpt": "...",
        "summary": "...",
        "url": "...",
        "kind": "post" | "comment" | "reaction",
      }
    """
    client = _client()
    if client is None:
        return None

    text = _item_to_text(item)
    if not text:
        return None
    if len(text) > MAX_SNIPPET_CHARS:
        text = text[:MAX_SNIPPET_CHARS]

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=_system_prompt(
                vendor_pitch=vendor_pitch,
                competitor_list=competitor_list,
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text if response.content else ""
    except Exception as e:
        logger.warning("person_intent classify failed: %s", e)
        return None

    decision = _parse_decision(raw)
    if not decision:
        return None
    if not decision.get("is_signal_bearing"):
        return None
    if decision.get("signal_type") not in _SIGNAL_TYPES:
        return None
    confidence = float(decision.get("confidence") or 0)
    if confidence < CONFIDENCE_FLOOR:
        return None

    return {
        "signal_type": decision["signal_type"],
        "confidence": confidence,
        "quoted_excerpt": (decision.get("quoted_excerpt") or "")[:280],
        "summary": (decision.get("summary") or "")[:200],
        "url": item.get("url") or "",
        "kind": item.get("kind") or "",
        # Original content, kept so the dashboard can render the post
        # itself as a LinkedIn-style card instead of a bare headline
        # (2026-07-07). Posts carry `text`; activity items carry the
        # reacted-to/commented-on post + the person's own comment.
        "post_text": (item.get("text") or item.get("original_post_text") or "")[:1200],
        "original_post_author": item.get("original_post_author") or "",
        "comment_text": (item.get("comment_text") or "")[:500],
        "posted_at": item.get("posted_at"),
        "engagement": item.get("engagement") or {},
    }


def classify_items_parallel(
    items: List[Dict[str, Any]],
    *,
    vendor_pitch: str,
    competitor_list: List[str],
    max_workers: int = 6,
) -> List[Dict[str, Any]]:
    """Run the classifier in parallel across items. Returns only the
    items that passed the signal-bearing + confidence filter."""
    if not items:
        return []
    out: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                classify_item,
                item,
                vendor_pitch=vendor_pitch,
                competitor_list=competitor_list,
            ): item
            for item in items
        }
        for fut in as_completed(futures):
            try:
                decision = fut.result()
            except Exception as e:
                logger.warning("Classifier task failed: %s", e)
                continue
            if decision:
                out.append(decision)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _item_to_text(item: Dict[str, Any]) -> str:
    """Project an activity item to a single text payload for the
    classifier. Posts → just the text. Comments → the original post
    text + the comment text + author info."""
    kind = item.get("kind") or ""
    if kind == "post":
        return (item.get("text") or "").strip()
    if kind == "comment":
        original = (item.get("original_post_text") or "").strip()
        author = (item.get("original_post_author") or "").strip()
        comment = (item.get("comment_text") or "").strip()
        parts: List[str] = []
        if author:
            parts.append(f"Original post by {author}:")
        if original:
            parts.append(original)
        parts.append("---")
        parts.append(f"Person's comment: {comment}")
        return "\n".join(parts)
    if kind == "reaction":
        original = (item.get("original_post_text") or "").strip()
        author = (item.get("original_post_author") or "").strip()
        reaction = (item.get("reaction_type") or "reacted").strip()
        if not original:
            return ""
        parts: List[str] = []
        if author:
            parts.append(f"Original post by {author}:")
        parts.append(original)
        parts.append(f"---\nPerson {reaction} to this post.")
        return "\n".join(parts)
    return ""


def _parse_decision(raw: str) -> Optional[Dict[str, Any]]:
    """Robust JSON extract — handles model wrappers like ```json ... ```
    and trailing prose."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    # Try raw_decode-style: parse leading JSON object even with trailing prose
    try:
        decoder = json.JSONDecoder()
        obj, _idx = decoder.raw_decode(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Fallback: locate the first { ... } block manually
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
