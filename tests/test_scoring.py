"""
tests/test_scoring.py — Unit tests for MHS, DBS, and PIP scoring.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.signals.scoring import (
    technical_subscore, sentiment_subscore, macro_subscore,
    compute_mhs, compute_dbs, compute_pip, daily_trend
)
from src.models import MacroData, SentimentData


def _candles(n=60, trend="up") -> list:
    """
    Generate synthetic OHLCV candles for testing.
    Uses alternating +/- micro-moves around a macro trend so RSI
    stays in the 45-75 range rather than pinning to 0 or 100.
    """
    step = 0.4 if trend == "up" else -0.4
    base = 100.0
    result = []
    for i in range(n):
        # Alternate small retracements: 3 steps forward, 1 back
        micro = -0.1 if (i % 4 == 3) else 0.0
        c = base + i * step + micro
        result.append({"t": i * 3600, "o": c - 0.1, "h": c + 0.3, "l": c - 0.3,
                        "c": c, "v": 1_000_000.0})
    return result


def _neutral_macro() -> MacroData:
    return MacroData(vix=18.0, fed_rate=5.0, t10y=4.5, unrate=4.0, spread=-0.5)

def _calm_macro() -> MacroData:
    return MacroData(vix=14.0, fed_rate=3.0, t10y=4.5, unrate=3.8, spread=1.5)

def _panic_macro() -> MacroData:
    return MacroData(vix=35.0, fed_rate=5.5, t10y=3.0, unrate=5.5, spread=-2.5)

def _bullish_sent() -> SentimentData:
    return SentimentData(score=0.6, confidence="high", fear_greed=72,
                         direction_bias="long")

def _neutral_sent() -> SentimentData:
    return SentimentData(score=0.0, confidence="medium", fear_greed=50,
                         direction_bias="neutral")


class TestTechnicalSubscore:
    def test_uptrend_scores_higher_than_neutral(self):
        # Uptrend should score higher than a downtrend
        assert technical_subscore(_candles()) > technical_subscore(_candles(trend="down"))

    def test_downtrend_below_50(self):
        assert technical_subscore(_candles(60, "down")) < 50

    def test_empty_returns_neutral(self):
        assert technical_subscore([]) == 50.0

    def test_output_clamped_0_100(self):
        s = technical_subscore(_candles(50, "up"))
        assert 0 <= s <= 100


class TestSentimentSubscore:
    def test_bullish_above_50(self):
        assert sentiment_subscore(_bullish_sent()) > 50

    def test_neutral_near_50(self):
        s = sentiment_subscore(_neutral_sent())
        assert 45 <= s <= 55

    def test_low_confidence_dampened(self):
        s_high = sentiment_subscore(
            SentimentData(score=0.8, confidence="high", fear_greed=80))
        s_low  = sentiment_subscore(
            SentimentData(score=0.8, confidence="low",  fear_greed=80))
        assert s_high > s_low


class TestMacroSubscore:
    def test_calm_macro_above_55(self):
        assert macro_subscore(_calm_macro()) > 55

    def test_panic_macro_below_30(self):
        assert macro_subscore(_panic_macro()) < 30

    def test_output_clamped_0_100(self):
        s = macro_subscore(_calm_macro())
        assert 0 <= s <= 100


class TestComputeMHS:
    def test_blocked_when_vix_high(self):
        from src.config import VIX_BLOCK
        macro = MacroData(vix=VIX_BLOCK + 1)
        result = compute_mhs(_candles(30), _neutral_sent(), macro)
        assert result["blocked"] is True

    def test_good_conditions_score_above_55(self):
        # Good macro + bullish sentiment should push MHS above neutral midpoint
        result = compute_mhs(_candles(), _bullish_sent(), _calm_macro())
        assert result["score"] > 55
        assert not result["blocked"]

    def test_has_breakdown(self):
        result = compute_mhs(_candles(30), _neutral_sent(), _neutral_macro())
        assert "tech" in result["breakdown"]
        assert "sent" in result["breakdown"]
        assert "macro" in result["breakdown"]

    def test_score_0_to_100(self):
        result = compute_mhs(_candles(30), _neutral_sent(), _neutral_macro())
        assert 0 <= result["score"] <= 100


class TestComputePIP:
    def test_neutral_dbs_returns_half(self):
        assert compute_pip(0.0) == 0.50

    def test_max_long_below_070(self):
        assert compute_pip(1.0) <= 0.70

    def test_max_short_above_030(self):
        assert compute_pip(-1.0) >= 0.30

    def test_monotone(self):
        assert compute_pip(0.3) > compute_pip(0.0) > compute_pip(-0.3)


class TestDailyTrend:
    def test_uptrend_detected(self):
        assert daily_trend(_candles(30, "up")) == "up"

    def test_downtrend_detected(self):
        assert daily_trend(_candles(30, "down")) == "down"

    def test_empty_returns_neutral(self):
        assert daily_trend([]) == "neutral"
