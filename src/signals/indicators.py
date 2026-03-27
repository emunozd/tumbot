"""
signals/indicators.py — Technical indicator calculations.

Single Responsibility: pure mathematical functions over price series.
No I/O, no state, no side effects. Every function takes a list and returns a value.

Extended in this version with directional prediction indicators:
  - Heikin-Ashi candle transform
  - Fair Value Gap (FVG) detection
  - Break of Structure (BOS) / Change of Character (CHoCH)
  - RSI divergence
  - Volume Spread Analysis (VSA)
  - Fibonacci retracement levels
  - Candlestick pattern recognition (engulfing, hammer, shooting star, doji)
"""

import math
import statistics
from typing import Optional, List, Dict, Tuple


# ── Existing indicators (unchanged) ───────────────────────────────────────

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


# ── Log return and EV ──────────────────────────────────────────────────────

def log_return(p0: float, p1: float) -> float:
    """
    Logarithmic return ln(p1/p0).
    Arithmetic returns don't sum correctly across trades.
    """
    if p0 <= 0 or p1 <= 0:
        return 0.0
    return round(math.log(p1 / p0), 6)


def expected_value(pip: float, mkt_price: float) -> float:
    """
    Expected value per dollar invested.
    EV = pip * (1 - mkt_price) - (1 - pip) * mkt_price
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


# ── NEW: Heikin-Ashi ───────────────────────────────────────────────────────

def heikin_ashi(candles: List[dict]) -> List[dict]:
    """
    Transform OHLCV candles into Heikin-Ashi candles.

    HA smooths price action, making trends and reversals clearer.
    A bullish HA candle with no lower wick indicates strong uptrend.
    A bearish HA candle with no upper wick indicates strong downtrend.

    Returns list of HA candles with same keys as input (o, h, l, c, t, v).
    Requires at least 2 candles.
    """
    if len(candles) < 2:
        return []
    ha = []
    for i, c in enumerate(candles):
        ha_c = (c["o"] + c["h"] + c["l"] + c["c"]) / 4
        if i == 0:
            ha_o = (c["o"] + c["c"]) / 2
        else:
            ha_o = (ha[-1]["o"] + ha[-1]["c"]) / 2
        ha_h = max(c["h"], ha_o, ha_c)
        ha_l = min(c["l"], ha_o, ha_c)
        ha.append({
            "t": c.get("t", i),
            "o": round(ha_o, 4),
            "h": round(ha_h, 4),
            "l": round(ha_l, 4),
            "c": round(ha_c, 4),
            "v": c.get("v", 0),
        })
    return ha


def ha_signal(candles: List[dict]) -> float:
    """
    Score -1.0 to +1.0 from last 3 Heikin-Ashi candles.

    +1.0: 3 consecutive bullish HA with no/small lower wicks (strong uptrend)
    -1.0: 3 consecutive bearish HA with no/small upper wicks (strong downtrend)
     0.0: mixed or doji HA candles
    """
    if len(candles) < 5:
        return 0.0
    ha = heikin_ashi(candles)
    if len(ha) < 3:
        return 0.0

    last3 = ha[-3:]
    score = 0.0
    for c in last3:
        body  = abs(c["c"] - c["o"])
        if body == 0:
            continue
        if c["c"] > c["o"]:  # bullish
            lower_wick = min(c["o"], c["c"]) - c["l"]
            strength = 1.0 if lower_wick < body * 0.1 else 0.5
            score += strength
        else:  # bearish
            upper_wick = c["h"] - max(c["o"], c["c"])
            strength = 1.0 if upper_wick < body * 0.1 else 0.5
            score -= strength

    return round(max(-1.0, min(1.0, score / 3.0)), 3)


# ── NEW: Fair Value Gap (FVG) ──────────────────────────────────────────────

def fair_value_gaps(candles: List[dict], lookback: int = 20) -> Dict[str, float]:
    """
    Detect Fair Value Gaps in recent candles.

    A bullish FVG occurs when candle[i].low > candle[i-2].high —
    price jumped up leaving an unfilled gap. Price tends to return
    to fill it, but if it holds above, confirms bullish momentum.

    A bearish FVG occurs when candle[i].high < candle[i-2].low.

    Returns:
      bull_fvg_size: size of most recent unfilled bullish FVG (0 if none)
      bear_fvg_size: size of most recent unfilled bearish FVG (0 if none)
      signal: +1.0 (bullish FVG present), -1.0 (bearish), 0.0 (none)
    """
    if len(candles) < 3:
        return {"bull_fvg_size": 0.0, "bear_fvg_size": 0.0, "signal": 0.0}

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    last_price = candles[-1]["c"]

    bull_fvg = 0.0
    bear_fvg = 0.0

    for i in range(2, len(recent)):
        c_prev2 = recent[i - 2]
        c_curr  = recent[i]
        # Bullish FVG: gap between prev2.high and curr.low
        if c_curr["l"] > c_prev2["h"]:
            gap = c_curr["l"] - c_prev2["h"]
            # Only count if price hasn't filled it yet
            if last_price > c_prev2["h"]:
                bull_fvg = max(bull_fvg, gap)
        # Bearish FVG: gap between prev2.low and curr.high
        if c_curr["h"] < c_prev2["l"]:
            gap = c_prev2["l"] - c_curr["h"]
            if last_price < c_prev2["l"]:
                bear_fvg = max(bear_fvg, gap)

    if bull_fvg > bear_fvg and bull_fvg > 0:
        signal = min(1.0, bull_fvg / (last_price * 0.005))  # normalize by 0.5% of price
    elif bear_fvg > bull_fvg and bear_fvg > 0:
        signal = -min(1.0, bear_fvg / (last_price * 0.005))
    else:
        signal = 0.0

    return {
        "bull_fvg_size": round(bull_fvg, 4),
        "bear_fvg_size": round(bear_fvg, 4),
        "signal": round(signal, 3),
    }


# ── NEW: Break of Structure / Change of Character ─────────────────────────

def market_structure(candles: List[dict], swing_period: int = 5) -> Dict[str, str]:
    """
    Detect Break of Structure (BOS) and Change of Character (CHoCH).

    BOS: continuation — price breaks in the direction of the existing trend.
      Bullish BOS: new swing high above previous swing high.
      Bearish BOS: new swing low below previous swing low.

    CHoCH: reversal — price breaks against the existing trend.
      Bullish CHoCH: in downtrend, price breaks above a previous lower high.
      Bearish CHoCH: in uptrend, price breaks below a previous higher low.

    Uses rolling swing highs/lows with swing_period lookback on each side.
    Requires at least 3 × swing_period candles.

    Returns:
      bos:   "bullish" | "bearish" | "none"
      choch: "bullish" | "bearish" | "none"
      trend: "up" | "down" | "neutral"
    """
    min_candles = swing_period * 3
    if len(candles) < min_candles:
        return {"bos": "none", "choch": "none", "trend": "neutral"}

    # Find swing highs and lows
    def is_swing_high(candles, i, period):
        if i < period or i >= len(candles) - period:
            return False
        h = candles[i]["h"]
        return all(candles[j]["h"] < h for j in range(i - period, i + period + 1) if j != i)

    def is_swing_low(candles, i, period):
        if i < period or i >= len(candles) - period:
            return False
        l = candles[i]["l"]
        return all(candles[j]["l"] > l for j in range(i - period, i + period + 1) if j != i)

    swing_highs = [(i, candles[i]["h"]) for i in range(len(candles)) if is_swing_high(candles, i, swing_period)]
    swing_lows  = [(i, candles[i]["l"]) for i in range(len(candles)) if is_swing_low(candles, i, swing_period)]

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bos": "none", "choch": "none", "trend": "neutral"}

    # Current and previous swing points
    curr_high = swing_highs[-1][1]
    prev_high = swing_highs[-2][1]
    curr_low  = swing_lows[-1][1]
    prev_low  = swing_lows[-2][1]
    curr_price = candles[-1]["c"]

    # Determine trend from swing structure
    hh = curr_high > prev_high  # higher high
    hl = curr_low  > prev_low   # higher low
    lh = curr_high < prev_high  # lower high
    ll = curr_low  < prev_low   # lower low

    if hh and hl:
        trend = "up"
    elif lh and ll:
        trend = "down"
    else:
        trend = "neutral"

    # BOS detection (last 3 candles broke a swing level)
    bos = "none"
    if curr_price > prev_high and trend != "down":
        bos = "bullish"
    elif curr_price < prev_low and trend != "up":
        bos = "bearish"

    # CHoCH detection
    choch = "none"
    if trend == "up" and curr_price < prev_low:
        choch = "bearish"   # uptrend broken — potential reversal down
    elif trend == "down" and curr_price > prev_high:
        choch = "bullish"   # downtrend broken — potential reversal up

    return {"bos": bos, "choch": choch, "trend": trend}


def structure_signal(candles: List[dict]) -> float:
    """
    Convert market structure analysis to -1.0 to +1.0 score.
    CHoCH (reversal) weighted more than BOS (continuation) for D+1 prediction.
    """
    ms = market_structure(candles)
    score = 0.0

    # Trend baseline
    if ms["trend"] == "up":   score += 0.3
    elif ms["trend"] == "down": score -= 0.3

    # BOS confirmation
    if ms["bos"] == "bullish":  score += 0.3
    elif ms["bos"] == "bearish": score -= 0.3

    # CHoCH reversal (strong signal for next-day direction)
    if ms["choch"] == "bullish":  score += 0.5
    elif ms["choch"] == "bearish": score -= 0.5

    return round(max(-1.0, min(1.0, score)), 3)


# ── NEW: RSI Divergence ────────────────────────────────────────────────────

def rsi_divergence(candles: List[dict], rsi_period: int = 14,
                   lookback: int = 20) -> float:
    """
    Detect RSI divergence vs price for D+1 directional prediction.

    Bullish divergence: price makes lower low, RSI makes higher low.
      → Momentum turning up despite price weakness → likely bounce.
    Bearish divergence: price makes higher high, RSI makes lower high.
      → Momentum weakening despite price strength → likely reversal.

    Returns -1.0 to +1.0:
      +1.0 = strong bullish divergence
      -1.0 = strong bearish divergence
       0.0 = no divergence
    """
    min_bars = rsi_period + lookback + 1
    if len(candles) < min_bars:
        return 0.0

    recent = candles[-lookback:]
    prices = [c["c"] for c in recent]
    rsi_vals = []
    all_prices = [c["c"] for c in candles]
    for i in range(len(candles) - lookback, len(candles)):
        r = rsi(all_prices[:i+1], rsi_period)
        rsi_vals.append(r if r is not None else 50.0)

    if len(prices) < 4 or len(rsi_vals) < 4:
        return 0.0

    # Find local lows and highs in price and RSI over the lookback window
    def local_extremes(series, n=3):
        """Find indices of local mins and maxs."""
        lows, highs = [], []
        for i in range(n, len(series) - n):
            if all(series[i] <= series[j] for j in range(i-n, i+n+1) if j != i):
                lows.append(i)
            if all(series[i] >= series[j] for j in range(i-n, i+n+1) if j != i):
                highs.append(i)
        return lows, highs

    price_lows, price_highs = local_extremes(prices)
    rsi_lows, rsi_highs = local_extremes(rsi_vals)

    score = 0.0

    # Bullish divergence: last two price lows descending, RSI lows ascending
    if len(price_lows) >= 2 and len(rsi_lows) >= 2:
        pl1, pl2 = price_lows[-2], price_lows[-1]
        rl1, rl2 = rsi_lows[-2], rsi_lows[-1]
        if (prices[pl2] < prices[pl1] and rsi_vals[rl2] > rsi_vals[rl1]):
            # Strength based on RSI divergence magnitude
            rsi_improvement = rsi_vals[rl2] - rsi_vals[rl1]
            score = min(1.0, rsi_improvement / 10.0)

    # Bearish divergence: last two price highs ascending, RSI highs descending
    if len(price_highs) >= 2 and len(rsi_highs) >= 2:
        ph1, ph2 = price_highs[-2], price_highs[-1]
        rh1, rh2 = rsi_highs[-2], rsi_highs[-1]
        if (prices[ph2] > prices[ph1] and rsi_vals[rh2] < rsi_vals[rh1]):
            rsi_deterioration = rsi_vals[rh1] - rsi_vals[rh2]
            score = -min(1.0, rsi_deterioration / 10.0)

    return round(score, 3)


# ── NEW: Fibonacci Retracement ─────────────────────────────────────────────

def fibonacci_signal(candles: List[dict], lookback: int = 60) -> float:
    """
    Detect if current price is near a key Fibonacci retracement level.

    Uses the most significant swing high and low in the lookback window.
    Key levels: 0.236, 0.382, 0.500, 0.618, 0.786.

    Returns +1.0 if price is at bullish Fibonacci support (retrace in uptrend),
    -1.0 if at bearish Fibonacci resistance (retrace in downtrend), 0.0 otherwise.
    """
    if len(candles) < 30:
        return 0.0

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    highs  = [c["h"] for c in recent]
    lows   = [c["l"] for c in recent]

    swing_high = max(highs)
    swing_low  = min(lows)
    rango      = swing_high - swing_low

    if rango <= 0:
        return 0.0

    current = candles[-1]["c"]
    tolerance = rango * 0.02  # 2% of range considered "near" a level

    fib_levels = [0.236, 0.382, 0.500, 0.618, 0.786]

    # Is the overall trend up or down?
    # If swing high came after swing low: uptrend (retracement = support)
    high_idx = highs.index(swing_high)
    low_idx  = lows.index(swing_low)
    uptrend  = low_idx < high_idx

    for level in fib_levels:
        if uptrend:
            fib_price = swing_high - rango * level
            if abs(current - fib_price) < tolerance:
                # At retracement support in uptrend — bullish
                strength = 1.0 if level in [0.382, 0.618] else 0.6
                return round(strength, 3)
        else:
            fib_price = swing_low + rango * level
            if abs(current - fib_price) < tolerance:
                # At retracement resistance in downtrend — bearish
                strength = 1.0 if level in [0.382, 0.618] else 0.6
                return round(-strength, 3)

    return 0.0


# ── NEW: Volume Spread Analysis (VSA) ─────────────────────────────────────

def vsa_signal(candles: List[dict], period: int = 20) -> float:
    """
    Volume Spread Analysis — detects supply/demand imbalance.

    Bullish VSA: high volume day with close in upper third of range.
      Indicates institutional buying / demand absorption.
    Bearish VSA: high volume day with close in lower third of range.
      Indicates institutional selling / supply dumping.
    No signal: low volume or close in middle of range (indecision).

    Returns -1.0 to +1.0.
    """
    if len(candles) < period + 1:
        return 0.0

    vols = [c.get("v", 0) for c in candles]
    avg_vol = statistics.mean(vols[-period-1:-1]) if len(vols) > period else statistics.mean(vols)
    if avg_vol <= 0:
        return 0.0

    last = candles[-1]
    rng  = last["h"] - last["l"]
    if rng <= 0:
        return 0.0

    vol_ratio_val = last.get("v", 0) / avg_vol
    close_pos = (last["c"] - last["l"]) / rng  # 0=bottom, 1=top

    # Only signal on above-average volume (>1.2x average)
    if vol_ratio_val < 1.2:
        return 0.0

    if close_pos >= 0.67:
        # Close in upper third — demand (bullish)
        strength = min(1.0, (vol_ratio_val - 1.0) / 2.0)
        return round(strength, 3)
    elif close_pos <= 0.33:
        # Close in lower third — supply (bearish)
        strength = min(1.0, (vol_ratio_val - 1.0) / 2.0)
        return round(-strength, 3)

    return 0.0


# ── NEW: Candlestick Pattern Recognition ──────────────────────────────────

def candlestick_signal(candles: List[dict]) -> float:
    """
    Detect key single and two-candle reversal/continuation patterns on 1D.

    Patterns detected (bullish → positive score, bearish → negative):
      Bullish engulfing (+1.0), Bearish engulfing (-1.0)
      Hammer (+0.7), Shooting star (-0.7)
      Bullish doji reversal (+0.5), Bearish doji reversal (-0.5)
      Morning star (+0.8), Evening star (-0.8)

    Returns -1.0 to +1.0. Returns 0.0 if no pattern found.
    """
    if len(candles) < 3:
        return 0.0

    c0 = candles[-3]  # three candles ago
    c1 = candles[-2]  # previous candle
    c2 = candles[-1]  # current (most recent closed) candle

    body2    = abs(c2["c"] - c2["o"])
    rng2     = c2["h"] - c2["l"]
    if rng2 <= 0:
        return 0.0

    body1    = abs(c1["c"] - c1["o"])
    rng1     = c1["h"] - c1["l"] if c1["h"] > c1["l"] else 1
    bull2    = c2["c"] > c2["o"]
    bear2    = c2["c"] < c2["o"]
    bull1    = c1["c"] > c1["o"]
    bear1    = c1["c"] < c1["o"]

    lower_wick2 = min(c2["o"], c2["c"]) - c2["l"]
    upper_wick2 = c2["h"] - max(c2["o"], c2["c"])

    # ── Bullish Engulfing ──
    if bear1 and bull2:
        if c2["o"] < c1["c"] and c2["c"] > c1["o"] and body2 > body1 * 0.8:
            return 1.0

    # ── Bearish Engulfing ──
    if bull1 and bear2:
        if c2["o"] > c1["c"] and c2["c"] < c1["o"] and body2 > body1 * 0.8:
            return -1.0

    # ── Hammer (bullish reversal at bottom) ──
    if lower_wick2 >= body2 * 2 and upper_wick2 < body2 * 0.3 and body2 > 0:
        # More significant if prior trend was down
        prev_prices = [c["c"] for c in candles[-6:-1]]
        if len(prev_prices) >= 3 and prev_prices[-1] < prev_prices[0]:
            return 0.7

    # ── Shooting Star (bearish reversal at top) ──
    if upper_wick2 >= body2 * 2 and lower_wick2 < body2 * 0.3 and body2 > 0:
        prev_prices = [c["c"] for c in candles[-6:-1]]
        if len(prev_prices) >= 3 and prev_prices[-1] > prev_prices[0]:
            return -0.7

    # ── Doji (indecision — check context for direction) ──
    if body2 < rng2 * 0.1 and rng2 > 0:
        # Doji after downtrend = potential bullish reversal
        prev_prices = [c["c"] for c in candles[-6:-1]]
        if len(prev_prices) >= 3:
            if prev_prices[-1] < prev_prices[0]:
                return 0.5   # bullish doji reversal signal
            elif prev_prices[-1] > prev_prices[0]:
                return -0.5  # bearish doji reversal signal

    # ── Morning Star (3-candle bullish reversal) ──
    body0 = abs(c0["c"] - c0["o"])
    if (bear1 and  # first candle bearish
            body1 < body0 * 0.5 and  # second candle small body (indecision)
            bull2 and body2 > body0 * 0.5 and  # third candle bullish and large
            c2["c"] > (c0["o"] + c0["c"]) / 2):  # closes above midpoint of first
        return 0.8

    # ── Evening Star (3-candle bearish reversal) ──
    if (bull1 and
            body1 < body0 * 0.5 and
            bear2 and body2 > body0 * 0.5 and
            c2["c"] < (c0["o"] + c0["c"]) / 2):
        return -0.8

    return 0.0


# ── NEW: Support & Resistance levels ──────────────────────────────────────

def support_resistance_signal(candles: List[dict],
                               sr_lookback: int = 100,
                               touch_tolerance: float = 0.015) -> float:
    """
    Detect if current price is near a significant S&R level.

    Method: cluster swing highs/lows from the last sr_lookback candles.
    A level is significant if price has touched it 2+ times.
    Uses candles_hist for longer-term context.

    Returns +1.0 if price is bouncing off support,
            -1.0 if price is rejecting from resistance,
             0.0 if not near any significant level.
    """
    if len(candles) < 20:
        return 0.0

    recent = candles[-sr_lookback:] if len(candles) >= sr_lookback else candles
    current = candles[-1]["c"]

    # Collect all swing highs and lows as candidate S&R levels
    candidates = []
    n = 3  # swing period
    for i in range(n, len(recent) - n):
        # Swing high
        if all(recent[i]["h"] >= recent[j]["h"] for j in range(i-n, i+n+1) if j != i):
            candidates.append(recent[i]["h"])
        # Swing low
        if all(recent[i]["l"] <= recent[j]["l"] for j in range(i-n, i+n+1) if j != i):
            candidates.append(recent[i]["l"])

    if not candidates:
        return 0.0

    # Cluster nearby levels (within tolerance)
    tol = current * touch_tolerance
    clusters = []
    for price in sorted(candidates):
        placed = False
        for cluster in clusters:
            if abs(price - cluster["center"]) < tol:
                cluster["prices"].append(price)
                cluster["center"] = statistics.mean(cluster["prices"])
                placed = True
                break
        if not placed:
            clusters.append({"center": price, "prices": [price]})

    # Find significant levels (touched 2+ times)
    significant = [c for c in clusters if len(c["prices"]) >= 2]

    if not significant:
        return 0.0

    # Check if current price is near any significant level
    for level in significant:
        dist = abs(current - level["center"])
        if dist < tol:
            strength = min(1.0, len(level["prices"]) / 4.0)
            # Above the level = support bounce (bullish)
            # Below the level = resistance rejection (bearish)
            if current >= level["center"]:
                return round(strength * 0.8, 3)   # at support
            else:
                return round(-strength * 0.8, 3)  # at resistance

    return 0.0