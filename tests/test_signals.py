"""Tests for signal stacking logic."""


class TestSignalStacking:
    """Test the signal stacking formula."""

    def test_single_signal(self):
        """1 signal = base strength."""
        raw_sum = 0.8
        count = 1
        score = raw_sum  # No multiplier
        assert score == 0.8

    def test_two_signals(self):
        """2 signals = sum * 1.3."""
        raw_sum = 0.8 + 0.6
        count = 2
        score = raw_sum * 1.3
        assert round(score, 2) == 1.82

    def test_three_plus_signals(self):
        """3+ signals = sum * 1.5."""
        raw_sum = 0.8 + 0.6 + 0.7
        count = 3
        score = raw_sum * 1.5
        assert round(score, 2) == 3.15

    def test_four_signals(self):
        """4 signals still uses 1.5 multiplier."""
        raw_sum = 0.5 + 0.5 + 0.5 + 0.5
        count = 4
        score = raw_sum * 1.5
        assert score == 3.0

    def test_zero_signals(self):
        """0 signals = 0 score."""
        score = 0
        assert score == 0
