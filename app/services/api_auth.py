"""FastAPI dependency for Bearer-token authentication on the public API.

Used by every endpoint under /api/v1/public/*. Extracts the bearer
token from the Authorization header, looks it up in api_keys, and
attaches the resolved api_key + org context to the request.

Standard header form (industry convention):
    Authorization: Bearer vera_live_xxxxxxxx...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.database import get_supabase
from app.services.api_keys import validate_key

logger = logging.getLogger(__name__)

# auto_error=False so we can return our own 401 with a useful message
# (FastAPI's default 403 with "Not authenticated" is unhelpful).
_bearer = HTTPBearer(auto_error=False)


@dataclass
class ApiContext:
    """Resolved identity for an authenticated request.

    Returned by both the per-tenant Bearer dependency (`require_api_key`,
    used by the public API) and the internal X-API-Key dependency
    (`verify_api_key` in main.py). Carries enough to org-scope downstream
    work, or to flag the legacy shared-key path for migration logging.
    """
    api_key_id: Optional[str]
    org_id: Optional[str]
    key_name: str
    is_superadmin: bool = False

    def enforce_org(self, requested_org_id: str) -> None:
        """Raise 403 if this key isn't allowed to act on the given org.

        Superadmin (legacy shared-key) callers bypass the check — they
        need to keep working until every internal client has migrated.
        Per-tenant keys must match the request's `org_id`.
        """
        if self.is_superadmin:
            return
        if not requested_org_id or self.org_id != requested_org_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key does not have access to the requested org",
            )


async def require_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None,
) -> ApiContext:
    """Validate the Bearer token and return the resolved context.

    Raises 401 with explanatory `WWW-Authenticate` header on missing or
    invalid token — clients can distinguish missing vs invalid by the
    error body.
    """
    # Manual extract because Depends() with HTTPBearer is awkward to test;
    # easier to read header directly.
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Send: Authorization: Bearer <your_api_key>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth_header.split(None, 1)[1].strip()

    db = get_supabase()
    record = validate_key(db, token)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if record.get("_subscription_lapsed"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Your Vera subscription has ended — reactivate from your dashboard settings to restore API access.",
        )
    if record.get("_payment_required"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "Your Vera subscription needs setting up before access "
                f"continues — log in at {settings.DASHBOARD_URL}/dashboard to "
                "complete payment. Everything is exactly where you left it."
            ),
        )

    # Stash on request.state so the post-request middleware can log usage
    # without re-validating.
    request.state.api_key_id = record["id"]
    request.state.org_id = record["org_id"]

    return ApiContext(
        api_key_id=record["id"],
        org_id=record["org_id"],
        key_name=record.get("name", ""),
        is_superadmin=False,
    )


# ---------------------------------------------------------------------------
# Internal X-API-Key dependency (moved from main.py 2026-06-10 so routers
# can request the resolved ApiContext per-handler without importing main).
# ---------------------------------------------------------------------------
from fastapi import Security  # noqa: E402
from fastapi.security import APIKeyHeader  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.api_keys import KEY_BRAND_PREFIX  # noqa: E402

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# One-shot deprecation warning state for the legacy shared key. Logging
# every request would flood production logs; logging once-per-process is
# enough to surface that shared-key callers still need to migrate.
_shared_key_warned = False


def verify_api_key(
    api_key: Optional[str] = Security(api_key_header),
) -> ApiContext:
    """Validate the X-API-Key header.

    Accepts either:
      • A per-tenant key (``vera_live_*``) looked up in the ``api_keys``
        table — preferred. Returns an ``ApiContext`` bound to the key's
        org so handlers can call ``ctx.enforce_org(body.org_id)``.
      • The legacy shared ``API_SECRET_KEY`` — kept working as a
        backward-compatibility shim while callers migrate. Treated
        as a superadmin context (``is_superadmin=True``, no org binding)
        and logged once per process so the migration surface is visible.

    Returns ``ApiContext`` so downstream handlers can scope to the
    caller's org. Existing handlers that don't read the return value
    continue to work unchanged.
    """
    if not api_key:
        raise HTTPException(
            status_code=403,
            detail="Missing API key. Send: X-API-Key: <your_key>",
        )

    # Per-tenant key path
    if api_key.startswith(KEY_BRAND_PREFIX):
        record = validate_key(get_supabase(), api_key)
        if record is None:
            raise HTTPException(
                status_code=403, detail="Invalid or revoked API key"
            )
        if record.get("_subscription_lapsed"):
            raise HTTPException(
                status_code=402,
                detail="Your Vera subscription has ended — reactivate from your dashboard settings to restore API access.",
            )
        if record.get("_payment_required"):
            raise HTTPException(
                status_code=402,
                detail=(
                    "Your Vera subscription needs setting up before access "
                    f"continues — log in at {settings.DASHBOARD_URL}/dashboard to "
                    "complete payment. Everything is exactly where you left it."
                ),
            )
        return ApiContext(
            api_key_id=record["id"],
            org_id=record["org_id"],
            key_name=record.get("name") or "",
            is_superadmin=False,
        )

    # Legacy shared key path (deprecated; migration shim). During a key
    # rotation API_SECRET_KEY_OLD holds the outgoing key so both work
    # until every caller (dashboard, edge functions) has the new one.
    valid_shared = {settings.API_SECRET_KEY}
    if settings.API_SECRET_KEY_OLD:
        valid_shared.add(settings.API_SECRET_KEY_OLD)
    if settings.API_SECRET_KEY and api_key in valid_shared:
        global _shared_key_warned
        if not _shared_key_warned and not settings.SELF_HOSTED:
            logger.warning(
                "DEPRECATED: shared API_SECRET_KEY in use. Migrate callers "
                "(cron jobs, dashboard) to per-tenant API keys from the "
                "api_keys table so internal endpoints can org-scope. This "
                "warning is logged once per process."
            )
            _shared_key_warned = True
        return ApiContext(
            api_key_id=None,
            org_id=None,
            key_name="shared-superadmin",
            is_superadmin=True,
        )

    raise HTTPException(status_code=403, detail="Invalid API key")


def require_superadmin(
    api_key: Optional[str] = Security(api_key_header),
) -> ApiContext:
    """X-API-Key dependency for INTERNAL routers (watchlist, threats, leads,
    dashboard, icp, billing, api-key/webhook management, etc.).

    These endpoints accept an org_id from the request body/path and act on it.
    They are only ever called by the SHARED superadmin key — cron jobs and the
    dashboard's clara-api proxy (which authenticates the user's session +
    resolves their org server-side before calling the engine). Per-tenant
    customer keys (vera_live_*) must NEVER reach them: such a key could pass
    another org's id and the handler would trust it (RLS is bypassed by the
    service role, so there's no second line of defence). Customer keys belong
    to the public API (/api/v1/public/*) and MCP only — both org-scoped.

    So: accept ONLY a superadmin context; reject per-tenant keys with 403.
    This is the tenant-isolation boundary for the internal surface.
    """
    ctx = verify_api_key(api_key)  # validates the key (raises 403 if invalid)
    if not ctx.is_superadmin:
        raise HTTPException(
            status_code=403,
            detail="This endpoint requires an internal key; per-tenant API keys can only use the public API.",
        )
    return ctx


# Imported but unused signal — silence linter by exposing the bearer scheme
__all__ = [
    "require_api_key",
    "verify_api_key",
    "require_superadmin",
    "api_key_header",
    "ApiContext",
    "_bearer",
]


def hosted_only() -> None:
    """Dependency for hosted-service routes (Stripe signup, the marketing
    free report): they don't exist on a self-hosted engine."""
    if settings.SELF_HOSTED:
        raise HTTPException(status_code=404, detail="Not found")
