"""Claude API helper — shared by all services that call the AI.

Three things this helper does that the raw SDK doesn't:

1. **Prompt caching** by default. The system prompt is wrapped in a
   cache_control: ephemeral block. Cache hits last 5 minutes. Within a
   single brain run (or any back-to-back same-system-prompt call) every
   turn after the first reads from cache at ~10% the input price. The
   biggest single cost-saver available short of switching models.

2. **Model tiering** via the `model` parameter. Default is settings.CLAUDE_MODEL
   (Sonnet) for reasoning-heavy tasks. Pass `model=settings.CLAUDE_FAST_MODEL`
   for low-stakes structured calls (domain resolution, classification,
   verification) — Haiku is ~10× cheaper.

3. **Full token telemetry**. We log all four counts on every call —
   input_tokens, output_tokens, cache_creation_input_tokens,
   cache_read_input_tokens — so we can actually see what's expensive.
   The summed cost respects the cached-token discount.
"""

import logging
import time

import anthropic

from app.config import settings
from app.prompts.templates import SYSTEM_INSTRUCTION, parse_claude_json
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None

# Retry config for transient errors (529 overloaded, 500 internal, etc.)
MAX_RETRIES = 3
RETRY_DELAYS = [5, 15, 30]  # seconds between retries

# Per-million-token pricing. Used by estimate_call_cost so dashboards
# can show the cached-token discount accurately. Keep in sync with
# Anthropic's published rates.
# Cache writes are ~25% premium over fresh input; cache reads ~10%.
_PRICING_PER_MILLION = {
    "claude-sonnet-4": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4": {
        "input": 1.00,
        "output": 5.00,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    "claude-opus-4": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
}


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _pricing_for(model: str) -> Dict[str, float]:
    """Return per-million-token rates for the given model. Falls back to
    Sonnet pricing if the model family isn't recognised."""
    for prefix, prices in _PRICING_PER_MILLION.items():
        if model.startswith(prefix):
            return prices
    return _PRICING_PER_MILLION["claude-sonnet-4"]


def estimate_call_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate the dollar cost of a single Claude call given the four
    token counts the SDK returns. Used for telemetry + dashboards."""
    rates = _pricing_for(model)
    return (
        (input_tokens * rates["input"])
        + (output_tokens * rates["output"])
        + (cache_creation_tokens * rates["cache_write"])
        + (cache_read_tokens * rates["cache_read"])
    ) / 1_000_000


def call_claude(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    cache_system: bool = True,
    max_tokens: Optional[int] = None,
) -> dict:
    """Call Claude API and return parsed JSON dict.

    Args:
        prompt: User-message content.
        system: Optional system prompt; defaults to SYSTEM_INSTRUCTION.
        model: Override the default model. Pass settings.CLAUDE_FAST_MODEL
            for cheap Haiku-suited tasks.
        cache_system: When True (default), wraps the system prompt in
            a cache_control block. Subsequent calls with the same system
            prompt within ~5 minutes hit the cache at ~10% of input
            price. Set False for genuinely one-shot calls where caching
            would just incur the cache-write cost without any reads.

    Returns:
        Parsed JSON dict on success, or {"error": "..."} on failure.

    Retries up to 3 times on transient errors (529 overloaded, 500
    internal, etc.). Logs full token telemetry on success.
    """
    client = _get_client()
    chosen_model = model or settings.CLAUDE_MODEL
    system_text = system or SYSTEM_INSTRUCTION

    # Build system as a content-block list when caching, else pass as
    # string. Anthropic's API accepts either form; only the list form
    # supports cache_control.
    if cache_system and system_text:
        system_arg: Any = [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        system_arg = system_text

    messages = [{"role": "user", "content": prompt}]

    for attempt in range(MAX_RETRIES):
        try:
            response = client.messages.create(
                model=chosen_model,
                max_tokens=max_tokens or settings.CLAUDE_MAX_TOKENS,
                temperature=settings.CLAUDE_TEMPERATURE,
                system=system_arg,
                messages=messages,
            )

            raw_text = response.content[0].text

            # Full token telemetry — Anthropic returns usage as a typed
            # object. Cache fields exist even when caching is disabled
            # (zeros in that case).
            usage = response.usage
            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

            cost = estimate_call_cost(
                chosen_model, input_tokens, output_tokens, cache_creation, cache_read,
            )

            logger.info(
                "Claude call: model=%s input=%d output=%d cache_write=%d cache_read=%d cost=$%.4f",
                chosen_model, input_tokens, output_tokens, cache_creation, cache_read, cost,
            )

            parsed = parse_claude_json(raw_text)
            return parsed

        except (anthropic.InternalServerError, anthropic.APIStatusError) as e:
            status = getattr(e, "status_code", 0)
            if status in (529, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    "Claude API transient error (HTTP %d), retrying in %ds (attempt %d/%d)",
                    status, delay, attempt + 1, MAX_RETRIES,
                )
                time.sleep(delay)
                continue
            logger.error("Claude API failed after %d attempts: %s", attempt + 1, e)
            return {"error": str(e)}

        except Exception as e:
            logger.error("Claude API unexpected error: %s", e)
            return {"error": str(e)}

    return {"error": "Claude API failed after all retries"}
