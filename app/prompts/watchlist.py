"""Prompts for watchlist alert generation."""

WATCHLIST_ALERT_PROMPT = """You are a strategic sales intelligence analyst. A target account that our client is watching has shown a change. Generate a brief, actionable alert.

## Our client:
Company: {client_company}
Services: {client_services}
Their ICP: {icp_description}

## Target account being watched:
Company: {target_company} ({target_domain})
Target contact: {target_contact_name}, {target_contact_title}
Why they're watching: {watch_notes}
Priority: {priority}

## Change detected:
Type: {change_type}
Details: {change_details}

## Previous state:
{previous_state}

Generate a concise, actionable alert. Be specific about what changed and why it matters. If a specific person is named in the change details or as the target contact, name them (with their title) in suggested_action and approach_angle — the reader's question is WHO to contact, not just which account.

Respond ONLY with valid JSON, no other text:
{{
    "alert_title": "<short headline, max 10 words>",
    "alert_summary": "<2-3 sentence summary of what changed and why it matters to our client>",
    "suggested_action": "<specific action the client should take — e.g. 'Send a congratulatory message referencing their new hire', 'Reach out about compliance support before their competitor does'>",
    "approach_angle": "<a suggested opening line or angle for outreach based on this change>",
    "urgency": "<high|medium|low>",
    "relevance_to_client": "<why this change specifically matters for our client's services>"
}}"""


CONTACT_ACTIVITY_PROMPT = """Analyse these recent LinkedIn posts from a target contact our client is watching. Identify any signals that suggest a potential sales opportunity.

## Target contact:
Name: {contact_name}
Title: {contact_title}
Company: {company_name}

## Our client's services:
{client_services}

## Recent posts:
{posts_text}

Respond ONLY with valid JSON, no other text:
{{
    "posts_analysed": <number>,
    "relevant_signals_found": <true|false>,
    "signals": [
        {{
            "post_summary": "<brief summary of the relevant post>",
            "signal_type": "<growth|hiring|challenge|vendor_review|budget|strategy_change|event|other>",
            "relevance": "<why this matters for our client>",
            "suggested_action": "<what our client should do with this info>"
        }}
    ],
    "overall_sentiment": "<positive|neutral|negative>",
    "engagement_level": "<inactive|low|moderate|high>",
    "best_conversation_starter": "<suggested opening line based on their recent content>",
    "topics_they_care_about": ["<list of topics from their recent posts>"]
}}"""
