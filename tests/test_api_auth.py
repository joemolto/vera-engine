"""Unit tests for the per-tenant + legacy-shared X-API-Key auth path.

Covers ``app.services.api_auth.verify_api_key`` and ``ApiContext.enforce_org``. The
shared-key shim is a backward-compatibility path and should keep working
until every internal caller has been migrated to a per-tenant key.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.api_auth import verify_api_key
from app.services.api_auth import ApiContext


@pytest.fixture(autouse=True)
def reset_shared_key_warning():
    """Reset the one-shot warning flag between tests."""
    import app.services.api_auth as main_module
    main_module._shared_key_warned = False
    yield
    main_module._shared_key_warned = False


class TestVerifyApiKey:
    def test_missing_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            verify_api_key(api_key=None)
        assert exc.value.status_code == 403

    def test_invalid_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            verify_api_key(api_key="not-a-real-key")
        assert exc.value.status_code == 403

    @patch("app.services.api_auth.settings")
    def test_legacy_shared_key_returns_superadmin_ctx(self, mock_settings):
        mock_settings.API_SECRET_KEY = "shared-secret"
        ctx = verify_api_key(api_key="shared-secret")
        assert isinstance(ctx, ApiContext)
        assert ctx.is_superadmin is True
        assert ctx.org_id is None
        assert ctx.api_key_id is None

    @patch("app.services.api_auth.settings")
    def test_legacy_shared_key_logs_once(self, mock_settings, caplog):
        mock_settings.API_SECRET_KEY = "shared-secret"
        mock_settings.SELF_HOSTED = False  # hosted mode; self-hosted never warns
        with caplog.at_level("WARNING"):
            verify_api_key(api_key="shared-secret")
            verify_api_key(api_key="shared-secret")
            verify_api_key(api_key="shared-secret")
        deprecations = [r for r in caplog.records if "DEPRECATED" in r.getMessage()]
        assert len(deprecations) == 1

    @patch("app.services.api_auth.validate_key")
    @patch("app.services.api_auth.get_supabase")
    def test_per_tenant_key_returns_org_bound_ctx(self, mock_db, mock_validate):
        mock_db.return_value = MagicMock()
        mock_validate.return_value = {
            "id": "key-uuid",
            "org_id": "org-uuid",
            "name": "n8n production",
        }
        ctx = verify_api_key(api_key="vera_live_abc12345xyzsecret")
        assert isinstance(ctx, ApiContext)
        assert ctx.is_superadmin is False
        assert ctx.org_id == "org-uuid"
        assert ctx.api_key_id == "key-uuid"

    @patch("app.services.api_auth.validate_key")
    @patch("app.services.api_auth.get_supabase")
    def test_revoked_per_tenant_key_rejected(self, mock_db, mock_validate):
        mock_db.return_value = MagicMock()
        mock_validate.return_value = None  # validate_key returns None for revoked
        with pytest.raises(HTTPException) as exc:
            verify_api_key(api_key="vera_live_revokedkey")
        assert exc.value.status_code == 403

    @patch("app.services.api_auth.validate_key")
    @patch("app.services.api_auth.get_supabase")
    def test_lapsed_org_key_gets_402(self, mock_db, mock_validate):
        mock_db.return_value = MagicMock()
        mock_validate.return_value = {
            "id": "key-uuid", "org_id": "org-uuid", "name": "k",
            "_subscription_lapsed": True,
        }
        with pytest.raises(HTTPException) as exc:
            verify_api_key(api_key="vera_live_lapsedkey")
        assert exc.value.status_code == 402
        assert "ended" in exc.value.detail

    @patch("app.services.api_auth.validate_key")
    @patch("app.services.api_auth.get_supabase")
    def test_payment_required_org_key_gets_402_with_pay_prompt(self, mock_db, mock_validate):
        mock_db.return_value = MagicMock()
        mock_validate.return_value = {
            "id": "key-uuid", "org_id": "org-uuid", "name": "k",
            "_payment_required": True,
        }
        with pytest.raises(HTTPException) as exc:
            verify_api_key(api_key="vera_live_gatedkey")
        assert exc.value.status_code == 402
        assert "complete payment" in exc.value.detail


class TestApiContextEnforceOrg:
    def test_superadmin_bypasses_org_check(self):
        ctx = ApiContext(
            api_key_id=None,
            org_id=None,
            key_name="shared",
            is_superadmin=True,
        )
        # Should not raise even for an arbitrary org_id
        ctx.enforce_org("any-org-uuid")

    def test_per_tenant_key_must_match_request_org(self):
        ctx = ApiContext(
            api_key_id="k1",
            org_id="org-a",
            key_name="k",
            is_superadmin=False,
        )
        ctx.enforce_org("org-a")  # OK

        with pytest.raises(HTTPException) as exc:
            ctx.enforce_org("org-b")
        assert exc.value.status_code == 403

    def test_per_tenant_key_rejects_missing_org(self):
        ctx = ApiContext(
            api_key_id="k1",
            org_id="org-a",
            key_name="k",
            is_superadmin=False,
        )
        with pytest.raises(HTTPException) as exc:
            ctx.enforce_org("")
        assert exc.value.status_code == 403
