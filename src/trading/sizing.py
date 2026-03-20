"""
trading/sizing.py — Kelly criterion position sizing.

Single Responsibility: calculate how much USDC to bet, nothing else.
Pure functions — no state, no I/O.
"""

from src.config import (
    KELLY_FRACTION, MAX_POS_PCT, MIN_POS_USDC,
    KELLY_MIN_TRADES, KELLY_FALLBACK
)


def kelly_win_rate(recent_trades: list) -> float:
    """
    Compute win rate from recent trade history.
    Used to calibrate Kelly when enough history exists.
    """
    if len(recent_trades) < KELLY_MIN_TRADES:
        return 0.0   # signal: not enough history
    winners = sum(1 for t in recent_trades if t.get("pnl", 0) > 0)
    return winners / len(recent_trades)


def kelly_fraction_dynamic(pip: float, mkt_price: float,
                            recent_trades: list) -> float:
    """
    Compute the fraction of capital to invest using quarter-Kelly.

    Formula: f* = (p*b - q) / b
      p = our probability estimate (PIP, possibly adjusted by trade history)
      q = 1 - p
      b = net payout ratio = (1 - price) / price

    Adjustments:
      - If enough trade history exists, blend PIP with actual win rate.
        Weight grows from 0% (10 trades) to 50% (30+ trades).
      - Apply KELLY_FRACTION (quarter-Kelly) to limit variance.
      - Cap at MAX_POS_PCT to prevent overexposure.
      - Return 0 if edge is negative (never bet on negative EV).
    """
    if mkt_price <= 0 or mkt_price >= 1:
        return 0.0

    # Blend PIP with historical win rate if history is sufficient
    p = pip
    n = len(recent_trades)
    if n >= KELLY_MIN_TRADES:
        wr  = kelly_win_rate(recent_trades)
        w   = min(n / 30, 0.5)   # weight grows from 0 → 0.5 over first 30 trades
        p   = pip * (1 - w) + wr * w

    q = 1 - p
    b = (1 - mkt_price) / mkt_price

    f_full  = (p * b - q) / b
    if f_full <= 0:
        return 0.0

    return min(f_full * KELLY_FRACTION, MAX_POS_PCT)


def position_size_usdc(capital: float, pip: float, mkt_price: float,
                        recent_trades: list) -> float:
    """
    Calculate the USDC amount to invest in a position.

    Returns 0 if the calculated size is below MIN_POS_USDC
    (too small to be worth the gas cost on Polygon).
    """
    if capital <= 0:
        return 0.0

    frac  = kelly_fraction_dynamic(pip, mkt_price, recent_trades)
    if frac <= 0:
        return 0.0

    usdc = round(capital * frac, 2)
    return usdc if usdc >= MIN_POS_USDC else 0.0


def calc_stop_take(side: str, entry_price: float) -> tuple:
    """
    Calculate stop-loss and take-profit CLOB prices for a position.

    stop  = entry * (1 - STOP_LOSS_PCT)   e.g. 0.52 * 0.65 = 0.338
    take  = entry * (1 + TAKE_PROFIT_PCT)  e.g. 0.52 * 1.50 = 0.780

    Take-profit is capped at 0.97 — Polymarket fees near 1.0 eat the gain.
    Side is included for potential future asymmetric stop logic.
    """
    from src.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT
    stop = round(entry_price * (1 - STOP_LOSS_PCT),  3)
    take = round(entry_price * (1 + TAKE_PROFIT_PCT), 3)
    take = min(take, 0.97)
    return stop, take
