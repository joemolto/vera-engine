from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, Any


class Settings(BaseSettings):
    # extra="ignore" lets us add env vars without declaring them on Settings —
    # the STRIPE_PRICE_* ones are read via os.environ.get() inside billing.py
    # rather than being typed config.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SUPABASE_URL: str
    SUPABASE_KEY: str
    # arq/Redis job queue (Upstash). Empty = in-process fallback (local dev).
    REDIS_URL: str = ""
    ANTHROPIC_API_KEY: str
    INSTANTLY_API_KEY: str = ""
    APOLLO_API_KEY: str = ""
    APIFY_API_KEY: str = ""

    OPENROUTER_API_KEY: str = ""
    # HubSpot OAuth app creds — needed engine-side for token refresh
    # (the dashboard callback does the initial exchange).
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""
    # Slack app signing secret — verifies Events API requests.
    SLACK_SIGNING_SECRET: str = ""
    PREDICTLEADS_API_KEY: str = ""
    PREDICTLEADS_API_TOKEN: str = ""
    TAVILY_API_KEY: str = ""
    FINDYMAIL_API_KEY: str = ""
    # LeakRadar — stealer-log / dark-web credential exposure. Free searches
    # (we never unlock plain-text), so this only powers the Risk-axis
    # credential_leak signals. Absent key → credential_exposure enricher
    # no-ops cleanly (returns None, leaving prior summaries untouched).
    LEAK_RADAR_API_KEY: str = ""
    # UK Companies House — one of the three revenue-reconciliation
    # sources (with Apollo + Perplexity). The enricher reads this
    # directly via `settings.COMPANIES_HOUSE_API_KEY`; without it being
    # declared on Settings, Pydantic silently drops the env var (extra=
    # "ignore" above) and every CH call raises AttributeError, gets
    # swallowed by the Phase 1 exception handler, and the source is
    # silently absent from sources_completed.
    COMPANIES_HOUSE_API_KEY: str = ""
    API_SECRET_KEY: str
    # Zero-downtime key rotation: set this to the OUTGOING key while the
    # new one rolls out to every caller (dashboard, edge functions), then remove it.
    # Both keys authenticate while it's set.
    API_SECRET_KEY_OLD: str = ""
    WEBHOOK_SECRET: str = ""

    # Stripe billing
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    # Frontend origin for Stripe success/cancel redirects after Checkout
    STRIPE_CHECKOUT_RETURN_URL: str = "http://localhost:3000/dashboard"

    # Resend — transactional email delivery for customer-facing welcome,
    # password reset, etc. Bypasses Supabase's built-in templates so we
    # can ship slick branded HTML conditional on tier.
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = ""  # needed only to send email
    RESEND_FROM_NAME: str = "Vera"
    # Cal.com link surfaced as the "book your onboarding call" CTA inside
    # the welcome email.
    ONBOARDING_CAL_URL: str = ""
    # Public dashboard origin — used to build the "after-signup" redirect
    # URL inside Supabase generate_link. Set to your dashboard's public URL.
    DASHBOARD_URL: str = "http://localhost:3000"
    # Who receives operator alerts (stalled monitor, quiet orgs, new
    # signups). Empty = no alert emails.
    OPERATOR_EMAIL: str = ""

    # Self-hosted mode: one operator runs Vera with their own provider keys.
    # Lifts every plan cap (orgs are still provisioned as tier "solo" so they
    # get the full product); billing, trials and credits never come into play
    # because no Stripe flow creates or changes the org.
    SELF_HOSTED: bool = False
    # Self-hosted only: hour (UTC) of the daily monitor pass + digest email.
    MONITOR_HOUR_UTC: int = Field(4, ge=0, le=23)

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    # Interactive API docs (Swagger/ReDoc/openapi.json) disclose the whole
    # internal endpoint catalogue, so they are OFF by default and must be
    # explicitly enabled for local dev. Default-deny is deliberate: a server
    # can easily be left on ENVIRONMENT=development, so docs must NOT key off
    # that — an opt-in flag is the only safe gate.
    API_DOCS_ENABLED: bool = False

    # Sentry error tracking (Phase 0, 2026-07-07). Empty = disabled; set
    # the DSN in your .env to switch it on. Init is try/excepted in
    # main.py so a missing sentry-sdk package can never stop the engine.
    SENTRY_DSN: str = ""

    # Claude API defaults.
    # CLAUDE_MODEL is the default reasoning model (Sonnet) — used for
    # briefings, scoring, and the vera_brain agent loop where decision
    # quality matters.
    # CLAUDE_FAST_MODEL is Haiku — used for low-stakes structured tasks
    # (domain resolution, email classification, LinkedIn verification)
    # where it's ~10× cheaper and the quality gap is negligible.
    CLAUDE_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_FAST_MODEL: str = "claude-haiku-4-5-20251001"
    CLAUDE_MAX_TOKENS: int = 4096
    CLAUDE_TEMPERATURE: float = 0.0


settings = Settings()
