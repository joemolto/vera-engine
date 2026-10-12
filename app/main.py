import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.services.api_auth import api_key_header, verify_api_key, require_superadmin  # noqa: F401

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentry (Phase 0) — on only when SENTRY_DSN is set; never fatal. Captures
# unhandled exceptions AND logger.error calls (many engine failures are
# caught-and-logged rather than raised, so the logging integration is what
# actually surfaces them).
# ---------------------------------------------------------------------------
if settings.SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.ENVIRONMENT,
            traces_sample_rate=0.05,
            integrations=[
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
        )
        logger.info("Sentry initialised (%s)", settings.ENVIRONMENT)
    except Exception as e:  # missing package, bad DSN — never stop the engine
        logger.warning("Sentry init failed: %s", e)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
# Interactive docs + the OpenAPI schema disclose the full internal API
# surface (incl. admin + org-in-path routes). Default-deny (see config):
# set API_DOCS_ENABLED=true locally to turn them on.
_docs_enabled = settings.API_DOCS_ENABLED
app = FastAPI(
    title="Vera Engine",
    version="1.0.0",
    description="AI-powered GTM pipeline engine for cybersecurity companies",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# CORS: the engine is called server to server (edge functions, the
# dashboard's server routes, your own tools) with header API keys, never
# browser cookies, so no credentialed cross-origin requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth dependency — lives in app/services/api_auth.py so routers can
# request the resolved ApiContext per-handler (Depends(verify_api_key))
# without a circular import on main. Re-exported here because every
# include_router below references it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Register routers
# ---------------------------------------------------------------------------
from app.routers import (  # noqa: E402
    account_suggestions as account_suggestions_router,
    api_keys as api_keys_router,
    billing,
    brain,
    competitors as competitors_router,
    dashboard,
    enrichment,
    feedback as feedback_router,
    groups as groups_router,
    health,
    integrations_push as integrations_push_router,
    slack_events as slack_events_router,
    icp,
    leads,
    market as market_router,
    public_api,
    relevance,
    scoring,
    signals,
    ai_quality as ai_quality_router,
    threat_intel,
    usage,
    watchlist,
    webhook_endpoints as webhook_endpoints_router,
    webhooks,
)

# Unmounted 2026-06-10: content (Apify content engine, dormant
# since March), instantly (outreach push), replies (reply classifier).
# Nothing calls them, and Vera is intelligence-not-
# outreach, so they're off the attack surface. Code stays in
# app/routers/ if outreach integrations ever come back.

# Health — no auth
app.include_router(health.router, prefix="/api/v1")

# Webhooks — auth via query param, not X-API-Key
app.include_router(webhooks.router, prefix="/api/v1/webhooks")

# All other routers — require X-API-Key
# Watchlist — require X-API-Key, separate prefix
app.include_router(
    watchlist.router,
    prefix="/api/v1/watchlist",
    dependencies=[Depends(require_superadmin)],
)

# Groups — AI interpretation of a group name into targeting criteria. Called by
# the dashboard's server route (shared key).
app.include_router(
    groups_router.router,
    prefix="/api/v1/groups",
    dependencies=[Depends(require_superadmin)],
)

# Competitor watch — competitors' posts + security engagers. Cron/manual.
app.include_router(
    competitors_router.router,
    prefix="/api/v1/competitors",
    dependencies=[Depends(require_superadmin)],
)

# Threat intel — require X-API-Key, separate prefix
app.include_router(
    threat_intel.router,
    prefix="/api/v1/threats",
    dependencies=[Depends(require_superadmin)],
)

# Account suggestions — opt-in feature. Self-regulating candidate
# pool (Apollo org-search) gated on icp_config.features.account_suggestions.
# Endpoints no-op for orgs without the flag, so this is safe to mount
# above the per-org gate. See routers/account_suggestions.py.
app.include_router(
    account_suggestions_router.router,
    prefix="/api/v1/accounts",
    dependencies=[Depends(require_superadmin)],
)

# Your market — TAM + market radar. Gated per-org on
# icp_config.features.market_v1; endpoints answer {"enabled": false}
# for orgs without the flag. Same /accounts prefix so the clara-api
# proxy path is /accounts/market/*.
app.include_router(
    market_router.router,
    prefix="/api/v1/accounts",
    dependencies=[Depends(require_superadmin)],
)

# AI quality pass — backfill endpoint for accounts missing it.
app.include_router(
    ai_quality_router.router,
    prefix="/api/v1/ai-quality",
    dependencies=[Depends(require_superadmin)],
)

# ICP title-relevance verdicts — called by the apollo-people edge function so
# the live account-drawer people list is LLM-relevance judged + ranked.
app.include_router(
    relevance.router,
    prefix="/api/v1",
    dependencies=[Depends(require_superadmin)],
)

protected = [leads, scoring, signals, enrichment, dashboard, usage, brain, feedback_router, icp]
for module in protected:
    app.include_router(
        module.router,
        prefix="/api/v1",
        dependencies=[Depends(require_superadmin)],
    )

# Billing — auth-required endpoints (signup checkout, top-up, portal)
app.include_router(
    billing.router,
    prefix="/api/v1",
    dependencies=[Depends(require_superadmin)],
)

# Stripe webhook — no X-API-Key auth (Stripe sends signed requests instead);
# verified by signature inside the handler itself.
app.include_router(billing.webhook_router, prefix="/api/v1")

# Public Vera API — Bearer token auth per request (per-tenant API keys).
# No internal X-API-Key on this router; auth is per-handler via the
# require_api_key dependency declared in services/api_auth.py.
app.include_router(public_api.router, prefix="/api/v1")

# API key management endpoints (used by the dashboard) — gated by the
# existing internal X-API-Key like every other dashboard endpoint.
app.include_router(
    api_keys_router.router,
    prefix="/api/v1",
    dependencies=[Depends(require_superadmin)],
)

# Push to connected apps (HubSpot / Slack) — dashboard surface via
# clara-api (edge validates org membership); org-scoped reads inside.
app.include_router(
    integrations_push_router.router,
    prefix="/api/v1/integrations",
    dependencies=[Depends(require_superadmin)],
)

# Slack Events API (@Vera mentions/DMs) — NO X-API-Key: Slack calls it
# directly; every request is HMAC-verified against the signing secret
# inside the handler before anything is read.
app.include_router(
    slack_events_router.router,
    prefix="/api/v1/integrations/slack",
)

# Webhook endpoint management + delivery worker — gated by internal
# X-API-Key. Queued deliveries are flushed every 30s by the built-in
# scheduler (services/scheduler.py); the deliver-pending endpoint does
# the same on demand (see services/webhooks.py).
app.include_router(
    webhook_endpoints_router.router,
    prefix="/api/v1",
    dependencies=[Depends(require_superadmin)],
)


@app.on_event("startup")
def startup():
    logger.info("Vera Engine starting up (env=%s)", settings.ENVIRONMENT)
    if settings.SELF_HOSTED:
        # The admin key guards every internal route; refuse a guessable one.
        if len(settings.API_SECRET_KEY) < 32:
            raise RuntimeError("API_SECRET_KEY must be at least 32 characters (openssl rand -hex 32)")
        from app.services import scheduler
        scheduler.start()
