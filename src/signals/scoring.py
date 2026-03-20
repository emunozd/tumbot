"""
signals/scoring.py — Market Heat Score, Directional Bias Score, and PIP.

Single Responsibility: convert raw indicator values into trading scores.
No I/O. Takes data dicts, returns score dicts.
"""

from typing import Optional, Dict
from src.models import MacroData, SentimentData
from src.signals.indicators import technicals
from src.config import VIX_BLOCK


# ── Market Heat Score (MHS) ────────────────────────────────────────────────

def technical_subscore(candles_1h: list) -> float:
    """Score 0–100 from hourly technical indicators."""
    t   = technicals(candles_1h)
    rsi = t["rsi"]
    cur = t["cur"]
    ma5, ma20, ma50 = t["ma5"], t["ma20"], t["ma50"]
    mh  = t["macd"]["hist"]
    pb  = t["bb"]["pct_b"]
    vr  = t["vol_ratio"]

    if not any([rsi, cur, ma5]):
        return 50.0

    s = 50.0

    if rsi:
        if   rsi < 25: s += 20
        elif rsi < 35: s += 12
        elif rsi < 45: s += 5
        elif rsi > 75: s -= 20
        elif rsi > 65: s -= 12
        elif rsi > 55: s -= 5

    if cur and ma20: s += 10 if cur > ma20 else -10
    if ma5 and ma20: s += 8  if ma5 > ma20  else -8
    if cur and ma50: s += 5  if cur > ma50  else -5

    if mh is not None:
        if   mh > 0.05: s += 10
        elif mh > 0:    s += 5
        elif mh < -0.05:s -= 10
        elif mh < 0:    s -= 5

    if pb is not None:
        if   pb < 0.10: s += 8
        elif pb < 0.30: s += 3
        elif pb > 0.90: s -= 8
        elif pb > 0.70: s -= 3

    if vr:
        if   vr > 1.8: s += 7
        elif vr > 1.3: s += 3
        elif vr < 0.5: s -= 4

    cross = t["cross"]
    if cross == "golden": s += 8
    if cross == "death":  s -= 8

    return max(0.0, min(100.0, s))


def sentiment_subscore(sent: SentimentData) -> float:
    """Score 0–100 from NLP sentiment output."""
    nlp_norm  = ((sent.score + 1) / 2) * 100
    fg_val    = sent.fear_greed
    conf_mult = {"high": 1.0, "medium": 0.8, "low": 0.6}.get(sent.confidence, 0.7)
    raw = nlp_norm * 0.40 + fg_val * 0.60
    return round(50.0 + (raw - 50.0) * conf_mult, 1)


def macro_subscore(macro: MacroData) -> float:
    """Score 0–100 from FRED macro indicators."""
    s = 55.0

    vix = macro.vix
    if vix:
        if   vix < 13: s += 15
        elif vix < 18: s += 8
        elif vix < 22: s += 0
        elif vix < 25: s -= 8
        elif vix < 30: s -= 18
        else:          s -= 35

    sp = macro.spread
    if sp is not None:
        if   sp > 2.0:  s += 18
        elif sp > 1.0:  s += 12
        elif sp > 0.3:  s += 6
        elif sp > 0:    s += 2
        elif sp > -0.5: s -= 8
        elif sp > -1.0: s -= 15
        else:           s -= 22

    un = macro.unrate
    if un:
        if   un < 3.8: s += 8
        elif un < 4.5: s += 3
        elif un < 5.5: s -= 3
        else:          s -= 8

    return max(0.0, min(100.0, s))


def compute_mhs(candles_1h: list,
                sent: SentimentData,
                macro: MacroData) -> dict:
    """
    Compute the Market Heat Score (0–100).

    Weights:
      Technical  40% — RSI, MACD, MA cross, Bollinger, volume
      Sentiment  35% — NLP score, Fear & Greed estimate
      Macro      25% — VIX, yield curve spread, unemployment

    Returns a dict with score, zone, blocked flag, and sub-score breakdown.
    """
    tech  = technical_subscore(candles_1h)
    senti = sentiment_subscore(sent)
    macro_s = macro_subscore(macro)
    mhs   = round(tech * 0.40 + senti * 0.35 + macro_s * 0.25, 1)

    blocked = bool(macro.vix and macro.vix > VIX_BLOCK)

    if   blocked:   zone = "BLOCKED"
    elif mhs >= 81: zone = "BULL_STRONG"
    elif mhs >= 66: zone = "BULL"
    elif mhs >= 46: zone = "NEUTRAL"
    elif mhs >= 31: zone = "BEAR"
    else:           zone = "BEAR_STRONG"

    return {
        "score": mhs, "zone": zone, "blocked": blocked,
        "breakdown": {"tech": round(tech,1), "sent": round(senti,1),
                      "macro": round(macro_s,1)},
    }


# ── Directional Bias Score (DBS) ───────────────────────────────────────────

def compute_dbs(candles_1h: list,
                candles_1d: list,
                macro: MacroData,
                sent: SentimentData,
                day_change: float = 0.0) -> dict:
    """
    Compute the Directional Bias Score (-1 to +1).

    Four voters, each weighted:
      MA cross   30% — price vs MA5/MA20/MA50, golden/death cross
      Momentum   25% — RSI relative to 50, MACD histogram direction
      Volume     20% — volume ratio confirming price direction
      Macro      25% — VIX level, yield spread, NLP direction bias

    Returns score, direction (LONG/SHORT/NEUTRAL), agreement count, and votes.
    """
    t   = technicals(candles_1h)
    cur, ma5, ma20, ma50 = t["cur"], t["ma5"], t["ma20"], t["ma50"]
    rsi_v, cross, macd_d = t["rsi"], t["cross"], t["macd"]
    vr = t["vol_ratio"]

    votes = []

    # 1. MA cross (30%)
    ms = 0.0
    if ma5 and ma20 and cur:
        ms += 0.6 if (ma5 > ma20 and cur > ma20) else -0.6
        ms += 0.2 if cur > ma20 else -0.2
        if ma50: ms += 0.2 if cur > ma50 else -0.2
    if cross == "golden": ms = min(1.0, ms + 0.3)
    if cross == "death":  ms = max(-1.0, ms - 0.3)
    votes.append((max(-1, min(1, ms)), 0.30))

    # 2. Momentum (25%)
    mom = 0.0
    if rsi_v: mom += (rsi_v - 50) / 50
    if macd_d["hist"] is not None:
        h = macd_d["hist"]
        mom += max(-0.5, min(0.5, (1 if h > 0 else -1) * 0.4))
    if macd_d["macd"] and macd_d["signal"]:
        mom += 0.2 if macd_d["macd"] > macd_d["signal"] else -0.2
    votes.append((max(-1, min(1, mom)), 0.25))

    # 3. Volume direction (20%)
    vs = 0.0
    if vr and day_change:
        vs = (1 if day_change >= 0 else -1) * min(1.0, vr / 2.0)
    votes.append((vs, 0.20))

    # 4. Macro + sentiment (25%)
    macs = 0.0
    if macro.vix:
        macs += 0.4 if macro.vix < 18 else (-0.5 if macro.vix > 25 else 0)
    if macro.spread is not None:
        sp = macro.spread
        macs += 0.3 if sp > 0.5 else (0.1 if sp > 0 else (-0.4 if sp < -0.5 else 0))
    bias_map = {"strong_long": 0.5, "long": 0.3, "neutral": 0,
                "short": -0.3, "strong_short": -0.5}
    macs += bias_map.get(sent.direction_bias, 0)
    votes.append((max(-1, min(1, macs)), 0.25))

    dbs = max(-1.0, min(1.0, round(sum(s * w for s, w in votes), 3)))
    long_v  = sum(1 for s, _ in votes if s > 0.1)
    short_v = sum(1 for s, _ in votes if s < -0.1)
    agreement = max(long_v, short_v)

    from src.config import DBS_LONG_THRESH, DBS_SHORT_THRESH
    if   dbs >= DBS_LONG_THRESH  and agreement >= 3: direction = "LONG"
    elif dbs <= DBS_SHORT_THRESH and agreement >= 3: direction = "SHORT"
    else:                                             direction = "NEUTRAL"

    return {
        "score": dbs, "direction": direction, "agreement": agreement,
        "votes": {
            "ma_cross": round(votes[0][0], 3),
            "momentum": round(votes[1][0], 3),
            "volume":   round(votes[2][0], 3),
            "macro":    round(votes[3][0], 3),
        },
    }


# ── Implied Probability (PIP) ──────────────────────────────────────────────

def compute_pip(dbs_score: float) -> float:
    """
    Convert DBS to our own implied probability (0.30–0.70).
    Range is intentionally conservative — we never claim certainty.

    PIP = 0.50 + (DBS × 0.40)
    DBS=+1.0 → PIP=0.70  (maximum bullish estimate)
    DBS=-1.0 → PIP=0.30  (maximum bearish estimate)
    """
    return round(max(0.30, min(0.70, 0.50 + dbs_score * 0.40)), 3)


def daily_trend(candles_1d: list) -> str:
    """
    Determine the macro trend from daily candles.
    Used as a directional filter: don't buy YES in a downtrend.
    """
    prices = [c["c"] for c in candles_1d]
    from src.signals.indicators import sma
    ma5  = sma(prices, 5)
    ma20 = sma(prices, 20)
    cur  = prices[-1] if prices else None
    if not all([ma5, ma20, cur]):
        return "neutral"
    if ma5 > ma20 and cur > ma20: return "up"
    if ma5 < ma20 and cur < ma20: return "down"
    return "neutral"
