"""Prompts for the content engine — style analysis, voice profiling, and content generation."""

STYLE_ANALYSIS_PROMPT = """Analyse these LinkedIn posts from {person_name} ({person_context}) and extract their writing style patterns. I need a reusable style guide that another AI can follow to write content that mimics this person's voice and approach.

## Posts to analyse:
{posts_text}

Study the patterns carefully. Do NOT summarise the content — analyse HOW they write, not WHAT they write about.

Respond ONLY with valid JSON, no other text:
{{
    "sentence_style": "<description of typical sentence length and structure>",
    "paragraph_style": "<how they break up text — one line per sentence, long paragraphs, etc>",
    "hook_style": "<how they open posts — question, bold statement, story, contrarian take, etc>",
    "narrative_structure": "<how posts flow — problem-solution, storytelling, list-based, rant, observation-insight, etc>",
    "tone": "<overall tone — conversational, authoritative, provocative, humble, irreverent, etc>",
    "formality_level": "<casual, semi-formal, formal>",
    "perspective": "<first person, third person, addressing the reader directly, etc>",
    "emoji_usage": "<none, minimal, moderate, heavy>",
    "hashtag_usage": "<none, minimal at end, integrated, heavy>",
    "cta_style": "<how they end posts — question, call to action, statement, none>",
    "formatting_patterns": "<use of line breaks, bullet points, headers, bold text, etc>",
    "vocabulary_level": "<simple everyday words, industry jargon, mix of both>",
    "signature_phrases": ["<recurring phrases or expressions they use>"],
    "what_they_avoid": ["<things they clearly never do — e.g. no corporate speak, no emojis, no hashtags>"],
    "post_length_tendency": "<short (under 100 words), medium (100-300), long (300+)>",
    "engagement_tactics": "<how they drive comments — controversial takes, asking questions, sharing failures, etc>",
    "content_format_preference": "<pure text, text with image, carousel, video, poll>",
    "example_hooks": [
        "<3-5 example opening lines that capture their style>"
    ],
    "writing_rules": [
        "<5-8 specific rules an AI should follow to replicate this style>",
        "<e.g. 'Never use more than 8 words per sentence'>",
        "<e.g. 'Always start with a one-line hook that challenges conventional wisdom'>",
        "<e.g. 'Use line breaks after every sentence'>",
        "<e.g. 'End with a question, never a statement'>"
    ]
}}"""


VOICE_ANALYSIS_PROMPT = """Analyse these LinkedIn posts from {client_name} at {company_name}. Extract their personal voice, vocabulary, and natural communication patterns. This will be used to ensure AI-generated content suggestions sound like them, not like a generic AI.

## Their posts:
{posts_text}

Respond ONLY with valid JSON, no other text:
{{
    "natural_vocabulary": ["<words and phrases they naturally use>"],
    "industry_terms_they_use": ["<specific jargon or terminology>"],
    "topics_they_gravitate_toward": ["<themes they keep coming back to>"],
    "opinions_they_hold": ["<strong positions or beliefs they've expressed>"],
    "stories_they_tell": ["<types of anecdotes — client wins, personal lessons, industry observations>"],
    "how_they_reference_their_company": "<by name, 'we', 'my team', etc>",
    "how_they_reference_their_role": "<founder, MD, 'I run a...', etc>",
    "emotional_range": "<measured, passionate, frustrated, optimistic, etc>",
    "self_deprecation_level": "<none, occasional, frequent>",
    "vulnerability_level": "<guarded, occasionally open, very transparent>",
    "unique_perspective": "<what makes their viewpoint distinctive in their industry>",
    "voice_summary": "<2-3 sentence description of how this person sounds on LinkedIn>"
}}"""


CONTENT_GENERATION_PROMPT = """You are a LinkedIn content strategist for a cybersecurity company. Generate 3 LinkedIn post ideas for this week.

## CRITICAL RULES:
- You are NOT writing the posts. You are suggesting ideas with draft copy that the client will edit.
- The draft copy MUST follow the style guide below. Match the tone, sentence structure, formatting, and hooks exactly.
- The content MUST be relevant to the client's target buyers (their ICP).
- Each idea should come from a different angle: one from industry news, one from pipeline/market intelligence, one from thought leadership.
- Do NOT sound like AI. No "In today's rapidly evolving landscape." No "Here's the thing." No corporate buzzwords. Match the style guide.

## STYLE GUIDE TO FOLLOW:
{style_guide}

## CLIENT'S VOICE PROFILE:
{voice_profile}

## CLIENT'S CONTENT PREFERENCES:
{content_preferences}

## WHO THE CLIENT IS:
Company: {company_name}
Services: {services_description}
Their role: {client_role}

## WHO THEIR BUYERS ARE (ICP):
{icp_description}

## THIS WEEK'S INDUSTRY NEWS:
{industry_news}

## THIS WEEK'S PIPELINE INSIGHTS:
{pipeline_insights}

## CYBER THREAT INTELLIGENCE THIS WEEK:
{threat_intel_summary}

Consider whether any of these threats would make good content angles — positioning the client as an expert who can help their ICP navigate these threats.

Respond ONLY with valid JSON, no other text:
{{
    "suggestions": [
        {{
            "idea_number": 1,
            "angle": "<news_reactive|pipeline_insight|thought_leadership>",
            "topic": "<short topic description>",
            "why_now": "<why this is timely and relevant to their ICP this week>",
            "hook": "<the opening line of the post — must follow the style guide's hook pattern>",
            "draft_post": "<full draft post, 150-300 words, following the style guide exactly>",
            "cta": "<suggested call to action or closing question>",
            "target_persona": "<which ICP persona this would resonate with most>",
            "hashtags": ["<2-3 relevant hashtags, only if the style guide uses them>"],
            "best_day_to_post": "<Tuesday|Wednesday|Thursday>",
            "expected_engagement_driver": "<what about this post should drive comments>"
        }},
        {{
            "idea_number": 2,
            "angle": "...",
            "topic": "...",
            "why_now": "...",
            "hook": "...",
            "draft_post": "...",
            "cta": "...",
            "target_persona": "...",
            "hashtags": [],
            "best_day_to_post": "...",
            "expected_engagement_driver": "..."
        }},
        {{
            "idea_number": 3,
            "angle": "...",
            "topic": "...",
            "why_now": "...",
            "hook": "...",
            "draft_post": "...",
            "cta": "...",
            "target_persona": "...",
            "hashtags": [],
            "best_day_to_post": "...",
            "expected_engagement_driver": "..."
        }}
    ],
    "content_themes_this_week": ["<overarching themes connecting the 3 posts>"],
    "pipeline_connection": "<how these posts connect back to the client's active pipeline and ICP>"
}}"""
