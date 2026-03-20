"""
tests/test_sizing.py — Unit tests for Kelly position sizing.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.trading.sizing import (
    kelly_fraction_dynamic, position_size_usdc, calc_stop_take
)


def _make_trades(n_wins: int, n_losses: int) -> list:
    trades = [{"pnl":  1.0} for _ in range(n_wins)]
    trades += [{"pnl": -1.0} for _ in range(n_losses)]
    return trades


class TestKellyFraction:
    def test_positive_edge_returns_positive(self):
        f = kelly_fraction_dynamic(pip=0.65, mkt_price=0.45, recent_trades=[])
        assert f > 0

    def test_negative_edge_returns_zero(self):
        # pip < mkt_price → negative EV → should not bet
        f = kelly_fraction_dynamic(pip=0.40, mkt_price=0.60, recent_trades=[])
        assert f == 0.0

    def test_capped_at_max_pos_pct(self):
        from src.config import MAX_POS_PCT
        f = kelly_fraction_dynamic(pip=0.70, mkt_price=0.10, recent_trades=[])
        assert f <= MAX_POS_PCT

    def test_history_blends_win_rate(self):
        # 80% win rate history should push size up vs 0% history
        trades_good = _make_trades(24, 6)   # 80% win rate
        trades_bad  = _make_trades(6, 24)   # 20% win rate
        f_good = kelly_fraction_dynamic(0.55, 0.50, trades_good)
        f_bad  = kelly_fraction_dynamic(0.55, 0.50, trades_bad)
        assert f_good > f_bad


class TestPositionSize:
    def test_zero_capital_returns_zero(self):
        assert position_size_usdc(0.0, 0.65, 0.45, []) == 0.0

    def test_negative_edge_returns_zero(self):
        assert position_size_usdc(1000.0, 0.40, 0.60, []) == 0.0

    def test_below_minimum_returns_zero(self):
        # Very small capital → result below MIN_POS_USDC → 0
        assert position_size_usdc(10.0, 0.51, 0.50, []) == 0.0

    def test_normal_case_within_bounds(self):
        from src.config import MAX_POS_PCT, MIN_POS_USDC
        capital = 500.0
        size = position_size_usdc(capital, 0.65, 0.45, [])
        assert size >= MIN_POS_USDC
        assert size <= capital * MAX_POS_PCT


class TestStopTake:
    def test_stop_below_entry(self):
        sl, tp = calc_stop_take("YES", 0.52)
        assert sl < 0.52

    def test_take_above_entry(self):
        sl, tp = calc_stop_take("YES", 0.52)
        assert tp > 0.52

    def test_take_capped_at_097(self):
        # Even with very low entry, TP should not exceed 0.97
        _, tp = calc_stop_take("YES", 0.10)
        assert tp <= 0.97

    def test_loss_pct(self):
        from src.config import STOP_LOSS_PCT
        sl, _ = calc_stop_take("YES", 0.60)
        expected = round(0.60 * (1 - STOP_LOSS_PCT), 3)
        assert abs(sl - expected) < 0.001
