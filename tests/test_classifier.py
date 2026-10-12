"""Tests for reply classification service."""

from app.services.classifier import clean_reply_text


class TestCleanReplyText:
    """Test reply text cleaning."""

    def test_strips_html(self):
        html = "<p>Thanks for reaching out!</p>"
        result = clean_reply_text("", html)
        assert "Thanks for reaching out!" in result
        assert "<p>" not in result

    def test_removes_signature_separator(self):
        text = "I'm interested in learning more.\n\n--\nJohn Smith\nCEO, Acme Corp"
        result = clean_reply_text(text)
        assert "interested" in result
        assert "John Smith" not in result

    def test_removes_underscore_separator(self):
        text = "Let's schedule a call.\n___\nSent from my iPhone"
        result = clean_reply_text(text)
        assert "schedule" in result
        assert "iPhone" not in result

    def test_removes_quoted_reply(self):
        text = "Sounds good.\n\nOn March 15, 2026, Alex wrote:\n> Original message here"
        result = clean_reply_text(text)
        assert "Sounds good" in result
        assert "Original message" not in result

    def test_removes_quoted_lines(self):
        text = "Yes, let's connect.\n> Previous message\n> More quoted text"
        result = clean_reply_text(text)
        assert "let's connect" in result
        assert "Previous message" not in result

    def test_empty_reply_returns_empty(self):
        result = clean_reply_text("")
        assert result == ""

    def test_preserves_fresh_reply(self):
        text = "We're reviewing our security providers. Can you send more info?"
        result = clean_reply_text(text)
        assert result == text


class TestClassificationCategories:
    """Test that each classification maps to the right status."""

    def test_status_mapping(self):
        from app.services.classifier import CLASSIFICATION_STATUS_MAP

        assert CLASSIFICATION_STATUS_MAP["INTERESTED"] == "replied"
        assert CLASSIFICATION_STATUS_MAP["CURIOUS"] == "replied"
        assert CLASSIFICATION_STATUS_MAP["NOT_NOW"] == "replied"
        assert CLASSIFICATION_STATUS_MAP["UNSUBSCRIBE"] == "dead"
        assert CLASSIFICATION_STATUS_MAP["DEAD"] == "dead"
        assert CLASSIFICATION_STATUS_MAP["AUTO_REPLY"] is None
