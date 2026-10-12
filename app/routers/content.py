"""Content engine API endpoints — style analysis, voice profiling, content generation."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from app.database import get_supabase
from app.models.schemas import (
    ContentGenerateRequest,
    ContentPreferencesRequest,
    ScrapeAndAnalyseStyleRequest,
    ScrapeAndAnalyseVoiceRequest,
    StyleAnalysisRequest,
    VoiceAnalysisRequest,
)
from app.services.apify_client import scrape_linkedin_posts
from app.services.content.generator import generate_weekly_content
from app.services.content.style_analyser import analyse_client_voice, analyse_style

logger = logging.getLogger(__name__)

router = APIRouter(tags=["content"])


@router.post("/analyse-style")
def analyse_style_endpoint(req: StyleAnalysisRequest):
    """Ingest posts from a style reference person and produce a style guide.

    Stores the result in the org's content_config.style_reference.
    """
    db = get_supabase()

    posts = [p.dict() for p in req.posts]
    style_guide = analyse_style(
        posts=posts,
        person_name=req.person_name,
        person_context=req.person_context or "",
    )

    if "error" in style_guide:
        return {"status": "error", "detail": style_guide["error"]}

    # Store in org's content_config
    org_resp = db.table("organisations").select("content_config").eq(
        "id", req.org_id
    ).limit(1).execute()

    content_config = {}
    if org_resp.data:
        content_config = org_resp.data[0].get("content_config") or {}

    content_config["style_reference"] = {
        "name": req.person_name,
        "linkedin_url": req.linkedin_url,
        "posts_analysed": len(req.posts),
        "style_guide": style_guide,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

    db.table("organisations").update(
        {"content_config": content_config}
    ).eq("id", req.org_id).execute()

    logger.info("Style guide stored for org %s (reference: %s)", req.org_id, req.person_name)

    return {
        "status": "success",
        "person_name": req.person_name,
        "posts_analysed": len(req.posts),
        "style_guide": style_guide,
    }


@router.post("/analyse-voice")
def analyse_voice_endpoint(req: VoiceAnalysisRequest):
    """Ingest the client's own posts and produce a voice profile.

    Stores the result in the org's content_config.client_voice.
    """
    db = get_supabase()

    posts = [p.dict() for p in req.posts]
    voice_profile = analyse_client_voice(
        posts=posts,
        client_name=req.client_name,
        company_name=req.company_name,
    )

    if "error" in voice_profile:
        return {"status": "error", "detail": voice_profile["error"]}

    # Store in org's content_config
    org_resp = db.table("organisations").select("content_config").eq(
        "id", req.org_id
    ).limit(1).execute()

    content_config = {}
    if org_resp.data:
        content_config = org_resp.data[0].get("content_config") or {}

    content_config["client_voice"] = {
        "posts_analysed": len(req.posts),
        "voice_profile": voice_profile,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

    db.table("organisations").update(
        {"content_config": content_config}
    ).eq("id", req.org_id).execute()

    logger.info("Voice profile stored for org %s (client: %s)", req.org_id, req.client_name)

    return {
        "status": "success",
        "client_name": req.client_name,
        "posts_analysed": len(req.posts),
        "voice_profile": voice_profile,
    }


@router.post("/scrape-and-analyse-style")
def scrape_and_analyse_style_endpoint(req: ScrapeAndAnalyseStyleRequest):
    """Scrape LinkedIn posts via Apify and produce a style guide.

    Accepts a LinkedIn URL, scrapes their posts, and feeds them into the style analyser.
    """
    db = get_supabase()

    # Scrape posts via Apify
    scraped_posts = scrape_linkedin_posts(req.linkedin_url, max_posts=20, db=db)
    if not scraped_posts:
        return {"status": "error", "detail": "Could not scrape LinkedIn posts. Check the URL and APIFY_API_KEY."}

    # Convert to the format the style analyser expects
    posts = [
        {
            "text": p["text"],
            "date": p.get("date", ""),
            "likes": p.get("likes", 0),
            "comments": p.get("comments", 0),
        }
        for p in scraped_posts
    ]

    style_guide = analyse_style(
        posts=posts,
        person_name=req.person_name,
        person_context=req.person_context or "",
    )

    if "error" in style_guide:
        return {"status": "error", "detail": style_guide["error"]}

    # Store in org's content_config
    org_resp = db.table("organisations").select("content_config").eq(
        "id", req.org_id
    ).limit(1).execute()

    content_config = {}
    if org_resp.data:
        content_config = org_resp.data[0].get("content_config") or {}

    content_config["style_reference"] = {
        "name": req.person_name,
        "linkedin_url": req.linkedin_url,
        "posts_analysed": len(posts),
        "style_guide": style_guide,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

    db.table("organisations").update(
        {"content_config": content_config}
    ).eq("id", req.org_id).execute()

    logger.info("Style guide stored for org %s (reference: %s, scraped via Apify)", req.org_id, req.person_name)

    return {
        "status": "success",
        "person_name": req.person_name,
        "posts_scraped": len(scraped_posts),
        "posts_with_text": len(posts),
        "style_guide": style_guide,
    }


@router.post("/scrape-and-analyse-voice")
def scrape_and_analyse_voice_endpoint(req: ScrapeAndAnalyseVoiceRequest):
    """Scrape LinkedIn posts via Apify and produce a voice profile.

    Accepts a LinkedIn URL, scrapes their posts, and feeds them into the voice analyser.
    """
    db = get_supabase()

    # Scrape posts via Apify
    scraped_posts = scrape_linkedin_posts(req.linkedin_url, max_posts=20, db=db)
    if not scraped_posts:
        return {"status": "error", "detail": "Could not scrape LinkedIn posts. Check the URL and APIFY_API_KEY."}

    # Convert to the format the voice analyser expects
    posts = [
        {
            "text": p["text"],
            "date": p.get("date", ""),
            "likes": p.get("likes", 0),
            "comments": p.get("comments", 0),
        }
        for p in scraped_posts
    ]

    voice_profile = analyse_client_voice(
        posts=posts,
        client_name=req.client_name,
        company_name=req.company_name,
    )

    if "error" in voice_profile:
        return {"status": "error", "detail": voice_profile["error"]}

    # Store in org's content_config
    org_resp = db.table("organisations").select("content_config").eq(
        "id", req.org_id
    ).limit(1).execute()

    content_config = {}
    if org_resp.data:
        content_config = org_resp.data[0].get("content_config") or {}

    content_config["client_voice"] = {
        "posts_analysed": len(posts),
        "voice_profile": voice_profile,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }

    db.table("organisations").update(
        {"content_config": content_config}
    ).eq("id", req.org_id).execute()

    logger.info("Voice profile stored for org %s (client: %s, scraped via Apify)", req.org_id, req.client_name)

    return {
        "status": "success",
        "client_name": req.client_name,
        "posts_scraped": len(scraped_posts),
        "posts_with_text": len(posts),
        "voice_profile": voice_profile,
    }


@router.post("/preferences")
def set_preferences_endpoint(req: ContentPreferencesRequest):
    """Store the client's content preferences."""
    db = get_supabase()

    org_resp = db.table("organisations").select("content_config").eq(
        "id", req.org_id
    ).limit(1).execute()

    content_config = {}
    if org_resp.data:
        content_config = org_resp.data[0].get("content_config") or {}

    content_config["content_preferences"] = req.preferences

    db.table("organisations").update(
        {"content_config": content_config}
    ).eq("id", req.org_id).execute()

    logger.info("Content preferences stored for org %s", req.org_id)

    return {
        "status": "success",
        "preferences": req.preferences,
    }


@router.post("/generate")
def generate_content_endpoint(req: ContentGenerateRequest):
    """Generate this week's content suggestions.

    This is the main endpoint, meant for an external weekly cron.
    """
    db = get_supabase()

    result = generate_weekly_content(db, str(req.org_id))

    if "error" in result:
        return {"status": "error", "detail": result["error"]}

    return {
        "status": "success",
        "week_label": result.get("week_label"),
        "org_name": result.get("org_name"),
        "suggestions": result.get("suggestions", []),
        "content_themes_this_week": result.get("content_themes_this_week", []),
        "pipeline_connection": result.get("pipeline_connection", ""),
    }


@router.get("/suggestions/{org_id}")
def get_suggestions_endpoint(
    org_id: str,
    week: Optional[str] = Query(None, description="Week label e.g. 2026-W12"),
    limit: int = Query(1, description="Number of weeks to return"),
):
    """Return the latest content suggestions for a client."""
    db = get_supabase()

    query = db.table("content_suggestions").select("*").eq("org_id", org_id)

    if week:
        query = query.eq("week_label", week)

    query = query.order("generated_at", desc=True).limit(limit)

    resp = query.execute()

    return {
        "status": "success",
        "org_id": org_id,
        "count": len(resp.data) if resp.data else 0,
        "suggestions": resp.data or [],
    }
