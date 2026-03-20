"""
llm/analysis.py — LLM-powered market analysis.

Single Responsibility: build prompts and parse structured LLM responses.
Two tasks:
  1. Sentiment analysis of news headlines → SentimentData
  2. Bayesian PIP validation → adjusted probability
"""

import json
from typing import Optional
from src.models import SentimentData, MacroData
from src.llm.client import llm_chat
from src.config import MODEL_CONF_CAPS, LLM_MODEL


# ── Sentiment analysis ─────────────────────────────────────────────────────

_SENTIMENT_PROMPT = """
Analyze the sentiment of these US financial market headlines.

Headlines:
{headlines}

Return ONLY this JSON (no markdown, no explanation):
{{
  "score": -0.3,
  "confidence": "medium",
  "fear_greed_estimate": 42,
  "direction_bias": "neutral",
  "key_risk": "one sentence",
  "key_catalyst": "one sentence"
}}

score: -1.0 (extreme bearish) to +1.0 (extreme bullish).
fear_greed_estimate: 0-100.
direction_bias: strong_long | long | neutral | short | strong_short
confidence: high | medium | low
"""


def analyze_sentiment(headlines: list) -> SentimentData:
    """
    Run NLP sentiment analysis on a list of news headlines.
    Returns SentimentData with neutral defaults on failure.
    """
    if not headlines:
        return SentimentData()

    heads_text = "\n".join(f"- {n['headline']}" for n in headlines[:12])
    raw = llm_chat(_SENTIMENT_PROMPT.format(headlines=heads_text), max_tokens=350)

    if raw is None:
        return SentimentData()

    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        d = json.loads(raw)
        return SentimentData(
            score          = float(d.get("score", 0.0)),
            confidence     = d.get("confidence", "low"),
            fear_greed     = int(d.get("fear_greed_estimate", 50)),
            direction_bias = d.get("direction_bias", "neutral"),
            key_risk       = d.get("key_risk", ""),
            key_catalyst   = d.get("key_catalyst", ""),
        )
    except Exception:
        return SentimentData()


# ── Bayesian PIP validation ────────────────────────────────────────────────

_PIP_VALIDATION_PROMPT = """
You are a quantitative prediction market analyst.

Asset: {asset}
Current price: ${price:.2f}
Technical MHS (0-100): {mhs:.1f}
Directional DBS (-1 to +1): {dbs:+.2f}
Daily trend: {trend}
VIX: {vix:.2f}
NLP sentiment score: {nlp_score:+.2f}
NLP direction bias: {nlp_bias}
Our technical probability estimate (PIP): {pip:.2%}

Question: is our {pip:.0%} probability that {asset} goes UP today reasonable,
given the technical context and macro conditions you know about?

Consider:
- Base rate for crypto/equity daily up moves (~52-55% historically)
- Whether current macro context supports or contradicts the signal
- Adjust by at most ±0.10 from our estimate

Return ONLY JSON (no markdown):
{{"valid": true, "adjusted_pip": {pip:.3f}, "confidence": "medium", "reason": "one sentence"}}

Rules:
- adjusted_pip must be between 0.30 and 0.70
- confidence: high | medium | low
"""


def _model_conf_cap() -> float:
    """Return the maximum trust weight for the currently configured LLM."""
    model = LLM_MODEL.lower()
    for key, cap in MODEL_CONF_CAPS.items():
        if key in model:
            return cap
    return 0.50  # conservative default for unknown models


def validate_pip(asset: str, pip: float, mhs: float, dbs: float,
                 trend: str, macro: MacroData,
                 sent: SentimentData) -> dict:
    """
    Bayesian PIP validation using the LLM as a second opinion.

    Process:
      1. Technical analysis produces PIP = 0.65 (prior)
      2. LLM evaluates if that prior is reasonable given macro/news context
      3. We combine: pip_final = pip * (1 - w) + llm_pip * w
         where w is scaled by both LLM confidence and a per-model cap
         (smaller local models get less weight than GPT-4/Claude)

    Falls back to original PIP if LLM is unavailable or returns bad JSON.
    """
    raw = llm_chat(_PIP_VALIDATION_PROMPT.format(
        asset=asset, price=0,  # price is cosmetic context only
        mhs=mhs, dbs=dbs, trend=trend,
        vix=macro.vix or 20.0,
        nlp_score=sent.score,
        nlp_bias=sent.direction_bias,
        pip=pip,
    ), max_tokens=200)

    if raw is None:
        return {"valid": True, "adjusted_pip": pip, "confidence": "low",
                "reason": "LLM unavailable — using technical PIP"}

    try:
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)

        llm_pip = float(result.get("adjusted_pip", pip))
        llm_pip = max(0.30, min(0.70, llm_pip))

        # Weight = confidence level × model capability cap
        conf_base = {"high": 0.40, "medium": 0.25, "low": 0.10}.get(
            result.get("confidence", "low"), 0.15
        )
        weight    = conf_base * _model_conf_cap()
        final_pip = round(pip * (1 - weight) + llm_pip * weight, 3)
        final_pip = max(0.30, min(0.70, final_pip))

        return {
            "valid":        result.get("valid", True),
            "adjusted_pip": final_pip,
            "raw_llm_pip":  llm_pip,
            "confidence":   result.get("confidence", "low"),
            "reason":       result.get("reason", ""),
            "weight":       weight,
        }
    except Exception:
        return {"valid": True, "adjusted_pip": pip, "confidence": "low",
                "reason": "LLM parse error — using technical PIP"}
