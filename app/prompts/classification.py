from typing import Optional, Any
CLASSIFICATION_PROMPT = """You are an email reply classifier for a B2B cybersecurity sales team.

Classify this reply into ONE of these categories:
- INTERESTED — Clear buying interest, wants to talk, asks for meeting/demo/info
- CURIOUS — Somewhat interested, asks questions, non-committal but engaged
- NOT_NOW — Explicitly says not the right time, but leaves door open (e.g. "try me in Q3")
- OBJECTION — Raises a specific concern (budget, competitor, timing, authority)
- REFERRAL — Redirects to another person ("you should speak to our IT Director")
- UNSUBSCRIBE — Asks to be removed
- AUTO_REPLY — Out of office, auto-responder
- DEAD — Hard no, not interested, hostile

Also extract any intent signals from the reply:

## Reply to classify:
"{reply_text}"

## Context:
- Campaign: {campaign_name}
- Lead's role: {lead_title} at {lead_company}

Respond ONLY with valid JSON, no other text:
{{
    "classification": "<category>",
    "confidence": <0.0-1.0>,
    "extracted_intent": {{
        "buying_timeline": "<string or null>",
        "competitor_mentioned": "<string or null>",
        "objection": "<string or null>",
        "referral_to": "<string or null>",
        "follow_up_date": "<YYYY-MM-DD or null>",
        "sentiment": "<positive|neutral|negative>",
        "key_quote": "<most important phrase from reply>"
    }},
    "suggested_next_action": "<forward_to_client|schedule_followup|add_to_nurture|suppress|update_crm>"
}}"""
