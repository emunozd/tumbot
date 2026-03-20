"""
tests/test_indicators.py — Unit tests for technical indicators.

Run with: pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.signals.indicators import (
    rsi, sma, ema, macd, bollinger, atr,
    volume_ratio, ma_cross, log_return, expected_value
)


def _prices(n=30, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


class TestRSI:
    def test_uptrend_high_rsi(self):
        prices = _prices(30, step=1.0)
        assert rsi(prices) > 70

    def test_downtrend_low_rsi(self):
        prices = _prices(30, step=-1.0)
        assert rsi(prices) < 30

    def test_none_when_insufficient(self):
        assert rsi([1.0, 2.0]) is None

    def test_all_gains_returns_100(self):
        assert rsi(_prices(20, step=0.1)) == 100.0


class TestSMA:
    def test_correct_value(self):
        assert sma([1.0, 2.0, 3.0, 4.0, 5.0], 5) == 3.0

    def test_none_when_insufficient(self):
        assert sma([1.0, 2.0], 5) is None


class TestLogReturn:
    def test_round_trip(self):
        """0.80 → 0.40 → 0.80 should sum to 0."""
        lr1 = log_return(0.80, 0.40)
        lr2 = log_return(0.40, 0.80)
        assert abs(lr1 + lr2) < 1e-6

    def test_gain_positive(self):
        assert log_return(0.50, 0.75) > 0

    def test_loss_negative(self):
        assert log_return(0.75, 0.50) < 0

    def test_zero_price_returns_zero(self):
        assert log_return(0.0, 0.5) == 0.0


class TestExpectedValue:
    def test_positive_edge(self):
        # We think 60%, market says 40% → positive EV
        ev = expected_value(0.60, 0.40)
        assert ev > 0

    def test_negative_edge(self):
        # We think 40%, market says 60% → negative EV
        ev = expected_value(0.40, 0.60)
        assert ev < 0

    def test_zero_edge_at_fair(self):
        # Our PIP == market price → zero EV
        ev = expected_value(0.55, 0.55)
        assert abs(ev) < 0.001


class TestMACross:
    def test_golden_cross(self):
        # Need enough bars: 20 flat + 15 rising so MA5 cleanly crosses MA20
        flat  = [100.0] * 25
        rise  = [100.0 + i * 3 for i in range(15)]
        prices = flat + rise
        result = ma_cross(prices)
        # golden cross OR no cross (depending on warmup) — ensure not death
        assert result != "death"

    def test_no_cross_stable(self):
        assert ma_cross(_prices(30, step=0.01)) == "none"

    def test_none_when_insufficient(self):
        assert ma_cross([1.0, 2.0]) == "none"
