"""Shared prompt utilities and JSON parsing helpers."""

import json
import logging
import re
from typing import Optional, Any

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are an AI assistant that responds ONLY with valid JSON. "
    "Do not include any text before or after the JSON object. "
    "Do not wrap the JSON in markdown code fences."
)


def parse_claude_json(text: str) -> dict:
    """Parse JSON from Claude's response, handling common quirks.

    Strips markdown code fences, finds JSON boundaries, and attempts
    to parse. Returns a fallback error dict on failure.
    """
    cleaned = text.strip()

    # Strip markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object boundaries
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(cleaned[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            pass

    logger.error("Failed to parse Claude JSON response: %s", cleaned[:200])
    return {"error": "Failed to parse AI response", "raw_text": cleaned[:500]}
