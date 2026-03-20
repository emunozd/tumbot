"""
models.py — Pure data models (dataclasses only, zero logic).

Single Responsibility: define the shape of data that flows through the bot.
No imports from other bot modules — this is the base layer.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PolyPosition:
    """An open position on Polymarket."""
    asset:        str           # e.g. "BTC-USD"
    side:         str           # "YES" | "NO"
    token_id:     str           # CLOB token ID
    shares:       float         # number of shares held
    entry_price:  float         # price paid per share (0.00–1.00)
    usdc_spent:   float         # = shares × entry_price (maximum possible loss)
    entry_time:   datetime
    resolution:   str           # "daily" | "weekly"
    entry_mhs:    float         # Market Heat Score at entry
    entry_dbs:    float         # Directional Bias Score at entry
    entry_pip:    float         # our own implied probability at entry
    stop_loss:    float = 0.0   # CLOB price that triggers stop sell
    take_profit:  float = 0.0   # CLOB price that triggers profit take
    order_id:     str   = ""    # order ID returned by the CLOB
    status:       str   = "OPEN"
    pnl:          float = 0.0


@dataclass
class ClosedTrade:
    """
    Record of a completed trade — used for Kelly calibration and analytics.

    log_return is ln(exit/entry) which accumulates correctly across trades.
    pnl_pct is arithmetic return, kept only for display purposes.
    """
    asset:        str
    side:         str
    entry_price:  float
    exit_price:   float         # 1.0 = won, 0.0 = lost, market price = sold early
    shares:       float
    pnl:          float         # USDC profit/loss
    pnl_pct:      float         # arithmetic return % (display only)
    log_return:   float         # ln(exit/entry) — correct for aggregation
    ev_at_entry:  float         # expected value per dollar at entry time
    pip_at_entry: float         # PIP (our probability estimate) at entry time
    duration:     str           # human-readable duration string
    reason:       str           # WON | LOST | STOP_LOSS | TAKE_PROFIT | SIGNAL_REVERSED
    time:         str           # close timestamp string


@dataclass
class Signal:
    """
    The output of the signal engine for a single asset at a point in time.
    Combines MHS, DBS, PIP, and opportunity detection into one object.
    """
    asset:      str
    mhs:        float               # Market Heat Score 0–100
    dbs:        float               # Directional Bias Score -1 to +1
    direction:  str                 # LONG | SHORT | NEUTRAL
    pip:        float               # technical probability estimate
    pip_final:  float               # PIP after Bayesian LLM adjustment
    ev:         Optional[float]     # expected value per dollar (None if no market price)
    elr:        Optional[float]     # expected log return
    opportunity: Optional[dict]     # trade signal dict if entry conditions met, else None
    tf_trend:   str                 # daily trend: "up" | "down" | "neutral"
    mhs_blocked: bool = False       # True if VIX > threshold
    breakdown:  dict = field(default_factory=dict)  # tech/sent/macro sub-scores


@dataclass
class MacroData:
    """Snapshot of macro indicators from FRED."""
    vix:      Optional[float] = None
    fed_rate: Optional[float] = None
    t10y:     Optional[float] = None
    unrate:   Optional[float] = None
    spread:   Optional[float] = None    # T10Y - FED_RATE


@dataclass
class SentimentData:
    """NLP sentiment output from the LLM."""
    score:             float = 0.0      # -1.0 to +1.0
    confidence:        str   = "low"
    fear_greed:        int   = 50       # 0–100
    direction_bias:    str   = "neutral"
    key_risk:          str   = ""
    key_catalyst:      str   = ""
