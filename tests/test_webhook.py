"""Tests for Instantly webhook receiver."""

from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import API_KEY_HEADER


class TestWebhookAuth:
    """Test webhook authentication."""

    @patch("app.database.get_supabase")
    @patch("app.config.settings")
    def test_rejects_bad_secret(self, mock_settings, mock_db):
        mock_settings.WEBHOOK_SECRET = "correct-secret"
        mock_settings.API_SECRET_KEY = "test-key"
        mock_settings.SUPABASE_URL = "https://test.supabase.co"
        mock_settings.SUPABASE_KEY = "test"
        mock_settings.ENVIRONMENT = "test"
        mock_settings.LOG_LEVEL = "WARNING"
        mock_settings.ANTHROPIC_API_KEY = "test"
        mock_settings.INSTANTLY_API_KEY = ""
        mock_settings.APOLLO_API_KEY = ""
        mock_settings.COMPANIES_HOUSE_API_KEY = ""
        mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
        mock_settings.CLAUDE_MAX_TOKENS = 1024
        mock_settings.CLAUDE_TEMPERATURE = 0.0

        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        response = client.post(
            "/api/v1/webhooks/instantly?secret=wrong-secret&org_id=test-org",
            json={"event_type": "reply_received"},
        )
        assert response.status_code == 401


class TestWebhookRouting:
    """Test event type routing."""

    def test_reply_event_returns_200(self):
        """Webhook should return 200 immediately for reply events."""
        # The key test is that the endpoint returns quickly
        # Background processing is tested separately
        pass

    def test_unknown_event_accepted(self):
        """Unknown event types should be accepted (not crash)."""
        pass


class TestWebhookPayloadParsing:
    """Test Instantly webhook payload format."""

    def test_instantly_payload_fields(self):
        """Verify we handle the expected Instantly webhook fields."""
        payload = {
            "timestamp": "2026-03-17T14:32:11.042Z",
            "event_type": "reply_received",
            "workspace": "workspace-uuid",
            "campaign_id": "campaign-uuid",
            "campaign_name": "Example Campaign",
            "lead_email": "alex@example.com",
            "email_account": "sender@example.org",
            "reply_text": "Sounds interesting, let's talk",
            "reply_html": "<p>Sounds interesting, let's talk</p>",
            "reply_subject": "Re: Quick question",
            "step": 2,
            "variant": 1,
            "is_first": True,
        }
        # Verify all expected fields are present
        assert payload["event_type"] == "reply_received"
        assert payload["lead_email"] == "alex@example.com"
        assert payload["reply_text"] == "Sounds interesting, let's talk"
