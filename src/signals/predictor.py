"""
signals/predictor.py — Directional Predictor for D+1 price direction.

Single Responsibility: predict whether the asset price will be higher or lower
at the resolution time tomorrow, using multi-timeframe technical analysis.

This module replaces the MHS threshold as the primary entry gate.
MHS/DBS are preserved for display and monitoring purposes.

Architecture:
  DirectionalScore (-100 to +100) composed of 5 sub-scores:
    1. Market Structure  30% — BOS/CHoCH, S&R, trend
    2. Momentum          25% — RSI divergence, MACD histogram slope, HA
    3. Pattern           20% — Candlestick 1D, FVG, VSA
    4. Fibonacci         15% — Retracement levels
    5. Fundamental       10% — Sentiment + macro (reduced weight vs MHS)

Entry conditions (in engine.py):
  |DirectionalScore| >= 45   — minimum signal conviction
  direction matches (>0 for YES, <0 for NO)
  Polymarket YES price between 0.30 and 0.65  — market not already priced
  Current ET hour between 04:30 and 09:00     — early window, unpriced market
  VIX < VIX_BLOCK                             — not in panic mode
"""

from typing import Optional
from src.models import MacroData, SentimentData
from src.signals.indicators import (
    technicals, rsi, macd,
    ha_signal, fair_value_gaps, structure_signal,
    rsi_divergence, fibonacci_signal, vsa_signal,
    candlestick_signal, support_resistance_signal,
)
from src.data import database as DB


# ── Sub-score functions ────────────────────────────────────────────────────

def _structure_subscore(candles_1d: list) -> float:
    """
    Score -1.0 to +1.0 from market structure analysis on daily candles.
    Combines BOS/CHoCH signal with support/resistance proximity.
    Uses candles_hist for broader S&R context if available.
    """
    if len(candles_1d) < 20:
        return 0.0

    bos_choch = structure_signal(candles_1d)
    sr        = support_resistance_signal(candles_1d, sr_lookback=80)

    # Blend: structure signal is more forward-looking, weight it more
    return round(bos_choch * 0.65 + sr * 0.35, 3)


def _momentum_subscore(candles_1d: list, candles_1h: list) -> float:
    """
    Score -1.0 to +1.0 from momentum indicators.
    Combines RSI divergence (strongest signal), MACD histogram slope, HA.
    Uses 1D candles for divergence (D+1 relevant), 1H for HA (recent momentum).
    """
    score = 0.0
    weight_used = 0.0

    # RSI Divergence on 1D — most predictive for next-day direction
    if len(candles_1d) >= 35:
        div = rsi_divergence(candles_1d, lookback=25)
        score += div * 0.50
        weight_used += 0.50

    # Heikin-Ashi on 1H — captures recent intraday momentum direction
    if len(candles_1h) >= 5:
        ha = ha_signal(candles_1h[-20:] if len(candles_1h) >= 20 else candles_1h)
        score += ha * 0.30
        weight_used += 0.30

    # MACD histogram slope on 1D — is momentum accelerating or decelerating?
    if len(candles_1d) >= 35:
        prices_1d = [c["c"] for c in candles_1d]
        m = macd(prices_1d)
        if m["hist"] is not None:
            # Compare current hist to hist 3 bars ago
            prices_prev = [c["c"] for c in candles_1d[:-3]]
            m_prev = macd(prices_prev) if len(prices_prev) >= 35 else {"hist": None}
            if m_prev["hist"] is not None:
                slope = m["hist"] - m_prev["hist"]
                macd_score = max(-1.0, min(1.0, slope / abs(m["hist"] + 0.001) * 0.5))
                score += macd_score * 0.20
                weight_used += 0.20

    if weight_used == 0:
        return 0.0
    return round(max(-1.0, min(1.0, score / max(weight_used, 0.5))), 3)


def _pattern_subscore(candles_1d: list) -> float:
    """
    Score -1.0 to +1.0 from pattern analysis on daily candles.
    Combines candlestick patterns, FVG, and VSA.
    """
    if len(candles_1d) < 5:
        return 0.0

    cs  = candlestick_signal(candles_1d)
    fvg = fair_value_gaps(candles_1d, lookback=15)["signal"]
    vsa = vsa_signal(candles_1d)

    # Candlestick patterns are most direct signals
    score = cs * 0.50 + fvg * 0.30 + vsa * 0.20
    return round(max(-1.0, min(1.0, score)), 3)


def _fibonacci_subscore(candles_1d: list) -> float:
    """
    Score -1.0 to +1.0 from Fibonacci retracement analysis.
    Uses daily candles for swing high/low detection.
    """
    return fibonacci_signal(candles_1d, lookback=60)


def _fundamental_subscore(sent: SentimentData, macro: MacroData) -> float:
    """
    Score -1.0 to +1.0 from sentiment and macro context.
    Intentionally low weight (10%) — macro doesn't predict intraday direction
    but extreme conditions (panic VIX, strongly negative sentiment) matter.
    """
    score = 0.0

    # Sentiment direction
    if sent.score > 0.3:
        score += 0.5
    elif sent.score > 0.0:
        score += 0.2
    elif sent.score < -0.3:
        score -= 0.5
    elif sent.score < 0.0:
        score -= 0.2

    # Fear & Greed
    fg = sent.fear_greed
    if fg > 70:
        score += 0.3
    elif fg > 55:
        score += 0.1
    elif fg < 25:
        score -= 0.4
    elif fg < 40:
        score -= 0.1

    # VIX — only penalize extremes
    vix = macro.vix
    if vix:
        if vix < 15:
            score += 0.2
        elif vix > 30:
            score -= 0.5
        elif vix > 25:
            score -= 0.2

    return round(max(-1.0, min(1.0, score)), 3)


# ── Main DirectionalScore ──────────────────────────────────────────────────

def compute_directional_score(
    asset: str,
    candles_1h: list,
    candles_1d: list,
    sent: SentimentData,
    macro: MacroData,
) -> dict:
    """
    Compute the DirectionalScore for D+1 price direction prediction.

    Returns a dict with:
      score:      -100 to +100 (negative = bearish, positive = bullish)
      direction:  "UP" | "DOWN" | "NEUTRAL"
      conviction: "HIGH" | "MEDIUM" | "LOW" | "NONE"
      breakdown:  sub-scores for each component
      signal:     True if score magnitude >= threshold for entry consideration

    Score composition:
      structure  30% — BOS/CHoCH, S&R
      momentum   25% — RSI divergence, HA, MACD slope
      pattern    20% — Candlestick, FVG, VSA
      fibonacci  15% — Retracement levels
      fundamental 10% — Sentiment, macro (reduced weight)
    """
    # Try to load candles_hist for richer S&R context
    hist = []
    try:
        hist = DB.load_candles_1d(asset, limit=150)
        if not hist:
            hist = candles_1d
    except Exception:
        hist = candles_1d

    # Use hist for structure/fibonacci (longer context), 1d for patterns
    candles_for_structure = hist if len(hist) > len(candles_1d) else candles_1d
    candles_for_patterns  = candles_1d

    sub_structure   = _structure_subscore(candles_for_structure)
    sub_momentum    = _momentum_subscore(candles_1d, candles_1h)
    sub_pattern     = _pattern_subscore(candles_for_patterns)
    sub_fibonacci   = _fibonacci_subscore(candles_for_structure)
    sub_fundamental = _fundamental_subscore(sent, macro)

    # Weighted composite (-1.0 to +1.0)
    raw = (
        sub_structure   * 0.30 +
        sub_momentum    * 0.25 +
        sub_pattern     * 0.20 +
        sub_fibonacci   * 0.15 +
        sub_fundamental * 0.10
    )
    raw = round(max(-1.0, min(1.0, raw)), 4)

    # Scale to -100 to +100
    score = round(raw * 100, 1)

    # Direction and conviction
    abs_score = abs(score)
    if abs_score >= 60:
        conviction = "HIGH"
    elif abs_score >= 45:
        conviction = "MEDIUM"
    elif abs_score >= 25:
        conviction = "LOW"
    else:
        conviction = "NONE"

    if score >= 25:
        direction = "UP"
    elif score <= -25:
        direction = "DOWN"
    else:
        direction = "NEUTRAL"

    # Signal flag: minimum threshold for entry consideration
    # Engine will apply additional filters (price range, time window)
    signal = abs_score >= 45 and direction != "NEUTRAL"

    return {
        "score":      score,
        "direction":  direction,
        "conviction": conviction,
        "signal":     signal,
        "breakdown": {
            "structure":    round(sub_structure   * 100, 1),
            "momentum":     round(sub_momentum    * 100, 1),
            "pattern":      round(sub_pattern     * 100, 1),
            "fibonacci":    round(sub_fibonacci   * 100, 1),
            "fundamental":  round(sub_fundamental * 100, 1),
        },
    }
