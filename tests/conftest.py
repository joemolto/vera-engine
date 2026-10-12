"""Shared test fixtures — mock Supabase, Claude, and httpx."""

import json
import os
from unittest.mock import MagicMock, patch

# Settings (pydantic) requires these at import time. Real values come from
# .env locally; on CI (no .env) these stubs let the suite import. Tests
# never talk to real services — everything below is mocked.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret")

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Mock Supabase
# ---------------------------------------------------------------------------

class MockSupabaseQuery:
    """Chainable mock for Supabase query builder."""

    def __init__(self, data=None, count=0):
        self._data = data or []
        self._count = count

    def select(self, *args, **kwargs):
        return self

    def insert(self, *args, **kwargs):
        return self

    def update(self, *args, **kwargs):
        return self

    def upsert(self, *args, **kwargs):
        return self

    def delete(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def not_(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def lt(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        result = MagicMock()
        result.data = self._data
        result.count = self._count
        return result


class MockSupabase:
    """Mock Supabase client."""

    def __init__(self):
        self._tables: dict[str, list[dict]] = {}
        self._query_overrides: dict[str, list[dict]] = {}

    def set_table_data(self, table: str, data: list[dict]):
        self._query_overrides[table] = data

    def table(self, name: str) -> MockSupabaseQuery:
        data = self._query_overrides.get(name, [])
        return MockSupabaseQuery(data=data)


@pytest.fixture
def mock_db():
    """Provide a mock Supabase client."""
    return MockSupabase()


# ---------------------------------------------------------------------------
# Mock Claude
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_claude():
    """Patch call_claude to return a fixed response."""
    with patch("app.services.claude.call_claude") as mock:
        yield mock


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client(mock_db):
    """FastAPI TestClient with mocked dependencies."""
    with patch("app.database.get_supabase", return_value=mock_db):
        with patch("app.config.settings") as mock_settings:
            mock_settings.API_SECRET_KEY = "test-key"
            mock_settings.WEBHOOK_SECRET = "test-webhook-secret"
            mock_settings.ANTHROPIC_API_KEY = "test-anthropic-key"
            mock_settings.INSTANTLY_API_KEY = "test-instantly-key"
            mock_settings.APOLLO_API_KEY = "test-apollo-key"
            mock_settings.COMPANIES_HOUSE_API_KEY = "test-ch-key"
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_KEY = "test-key"
            mock_settings.ENVIRONMENT = "test"
            mock_settings.LOG_LEVEL = "WARNING"
            mock_settings.CLAUDE_MODEL = "claude-sonnet-4-20250514"
            mock_settings.CLAUDE_MAX_TOKENS = 1024
            mock_settings.CLAUDE_TEMPERATURE = 0.0
            mock_settings.SELF_HOSTED = False  # a bare MagicMock attr is truthy

            from app.main import app
            yield TestClient(app)


API_KEY_HEADER = {"X-API-Key": "test-key"}
