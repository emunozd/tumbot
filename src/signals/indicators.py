"""
signals/indicators.py — Technical indicator calculations.

Single Responsibility: pure mathematical functions over price series.
No I/O, no state, no side effects. Every function takes a list and returns a value.
"""

import math
import statistics
from typing import Optional, List, Dict


def rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not losses: return 100.0
    if not gains:  return 0.0
    ag = statistics.mean(gains[-period:])
    al = statistics.mean(losses[-period:])
    return round(100 - 100 / (1 + ag / al), 1) if al else 100.0


def sma(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return round(statistics.mean(prices[-period:]), 4)


def ema(prices: List[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k, e = 2 / (period + 1), statistics.mean(prices[:period])
    for p in prices[period:]:
        e = p * k + e * (1 - k)
    return round(e, 4)


def macd(prices: List[float]) -> Dict[str, Optional[float]]:
    """Standard MACD: EMA12 - EMA26, signal = EMA9 of MACD line."""
    e12, e26 = ema(prices, 12), ema(prices, 26)
    if not e12 or not e26:
        return {"macd": None, "signal": None, "hist": None}
    if len(prices) < 35:
        return {"macd": round(e12 - e26, 4), "signal": None, "hist": None}

    macd_series = []
    for i in range(26, len(prices) + 1):
        a, b = ema(prices[:i], 12), ema(prices[:i], 26)
        if a and b:
            macd_series.append(a - b)

    sig  = ema(macd_series, 9) if len(macd_series) >= 9 else None
    mv   = macd_series[-1] if macd_series else None
    hist = round(mv - sig, 4) if mv and sig else None
    return {"macd": round(mv, 4) if mv else None, "signal": sig, "hist": hist}


def bollinger(prices: List[float], period: int = 20, k: float = 2.0) -> Dict[str, Optional[float]]:
    if len(prices) < period:
        return {"upper": None, "middle": None, "lower": None, "pct_b": None}
    w   = prices[-period:]
    mid = statistics.mean(w)
    std = statistics.stdev(w)
    up  = mid + k * std
    lo  = mid - k * std
    pb  = (prices[-1] - lo) / (up - lo) if up != lo else 0.5
    return {"upper": round(up,4), "middle": round(mid,4),
            "lower": round(lo,4), "pct_b": round(pb,3)}


def atr(candles: List[dict], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = [
        max(c["h"] - c["l"],
            abs(c["h"] - candles[i-1]["c"]),
            abs(c["l"] - candles[i-1]["c"]))
        for i, c in enumerate(candles) if i > 0
    ]
    return round(statistics.mean(trs[-period:]), 4)


def volume_ratio(candles: List[dict], period: int = 20) -> Optional[float]:
    vols = [c["v"] for c in candles if c.get("v", 0) > 0]
    if len(vols) < period + 1:
        return None
    avg = statistics.mean(vols[-period-1:-1])
    return round(vols[-1] / avg, 2) if avg > 0 else None


def ma_cross(prices: List[float]) -> str:
    """Detect golden cross (MA5 crosses above MA20) or death cross."""
    if len(prices) < 22:
        return "none"
    m5n,  m20n  = sma(prices,      5), sma(prices,      20)
    m5p,  m20p  = sma(prices[:-1], 5), sma(prices[:-1], 20)
    if not all([m5n, m20n, m5p, m20p]):
        return "none"
    if m5p <= m20p and m5n > m20n: return "golden"
    if m5p >= m20p and m5n < m20n: return "death"
    return "none"


def technicals(candles: List[dict]) -> dict:
    """
    Compute all indicators from a candle list.
    Returns a flat dict — consumers pick what they need.
    """
    prices = [c["c"] for c in candles]
    return {
        "cur":       prices[-1] if prices else None,
        "rsi":       rsi(prices),
        "ma5":       sma(prices, 5),
        "ma20":      sma(prices, 20),
        "ma50":      sma(prices, 50),
        "ema9":      ema(prices, 9),
        "macd":      macd(prices),
        "bb":        bollinger(prices),
        "vol_ratio": volume_ratio(candles),
        "atr":       atr(candles),
        "cross":     ma_cross(prices),
    }


# ── Log return and EV (also pure functions) ────────────────────────────────

def log_return(p0: float, p1: float) -> float:
    """
    Logarithmic return ln(p1/p0).

    Why: arithmetic returns don't sum correctly across trades.
    Example: 0.80→0.40→0.80 is 0.000 log return (correct) but +50% arithmetic (wrong).
    """
    if p0 <= 0 or p1 <= 0:
        return 0.0
    return round(math.log(p1 / p0), 6)


def expected_value(pip: float, mkt_price: float) -> float:
    """
    Expected value per dollar invested.
    EV = pip * (1 - mkt_price) - (1 - pip) * mkt_price
    Positive = edge in our favour.
    """
    return round(pip * (1 - mkt_price) - (1 - pip) * mkt_price, 4)


def expected_log_return(pip: float, mkt_price: float) -> float:
    """Expected log return for a binary market position."""
    epsilon = 0.001
    if mkt_price <= 0 or mkt_price >= 1:
        return 0.0
    return round(
        pip * math.log(1.0 / mkt_price) +
        (1 - pip) * math.log(epsilon / mkt_price),
        6
    )
