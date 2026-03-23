"""
trading/engine.py — Signal evaluation and position lifecycle.

Single Responsibility: decide when to open and close positions.
Reads signals, applies filters, calls execution, persists via DB.
This is the orchestration layer — it delegates the actual work to the
other modules and wires them together.
"""

import math
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from src.config import (
    MHS_MIN_DAILY, MHS_MIN_WEEKLY, DBS_LONG_THRESH, DBS_SHORT_THRESH,
    EDGE_MIN, ENTRY_WINDOW_START, ENTRY_WINDOW_END, WATCH_ASSETS, ET
)
from src.models import PolyPosition, ClosedTrade, MacroData, SentimentData, Signal
from src.signals.indicators import log_return, expected_value
from src.signals.scoring import compute_mhs, compute_dbs, compute_pip, daily_trend
from src.llm.analysis import validate_pip
from src.trading.sizing import position_size_usdc, calc_stop_take
from src.trading import execution as CLOB
from src.data import database as DB


# ── Entry window ───────────────────────────────────────────────────────────

def in_entry_window(asset: str) -> bool:
    """
    All assets are always open if there is candle data available.

    Polymarket daily markets run 24/7 — equity ETF markets open at 16:01 ET
    the day before resolution and close at 16:00 ET on resolution day.
    There is no reason to block entry by time — if there is edge, we trade.
    Signal quality naturally drops outside NYSE hours (no fresh 1H candles)
    which lowers MHS organically. No artificial time gate needed.

    Only returns False if there is zero candle data (first run, no history yet).
    """
    cfg        = WATCH_ASSETS.get(asset, {})
    asset_type = cfg.get("asset_type", "equity_etf")

    if asset_type == "crypto":
        return True

    # Equity: open as long as we have candle data to base signals on
    from src.data import database as DB
    last_ts = DB.get_last_1d_ts(asset)
    return bool(last_ts and last_ts > 0)


# ── Opportunity detection ──────────────────────────────────────────────────

def detect_opportunity(asset: str, mhs: dict, dbs: dict,
                        pip_final: float, poly_prices: dict) -> Optional[dict]:
    """
    Compare our PIP against the current Polymarket price.
    Return a trade signal dict if the edge exceeds EDGE_MIN, else None.

    Mispricing logic:
      We believe P(BTC up) = 0.65
      Market prices YES at 0.52
      Edge = 0.65 - 0.52 = 0.13 > EDGE_MIN (0.08) → buy YES

    Additional filters:
      - MHS must exceed the threshold for the market's resolution
      - Signal must not be blocked (VIX too high)
      - Daily trend must not contradict the entry direction
      - Entry window must be open for this asset type
    """
    if mhs.get("blocked"):
        return None
    if not in_entry_window(asset):
        return None

    cfg     = WATCH_ASSETS.get(asset, {})
    mhs_min = MHS_MIN_WEEKLY if cfg.get("resolution") == "weekly" else MHS_MIN_DAILY
    if mhs["score"] < mhs_min:
        return None

    yes_price = poly_prices.get("yes")
    no_price  = poly_prices.get("no")
    if yes_price is None:
        return None

    direction = dbs["direction"]

    if direction == "LONG" and dbs["score"] >= DBS_LONG_THRESH:
        edge = pip_final - yes_price
        if edge >= EDGE_MIN:
            return {
                "side":      "YES",
                "edge":      round(edge, 3),
                "pip":       pip_final,
                "mkt_price": yes_price,
                "token_id":  cfg.get("token_yes", ""),
                "mhs":       mhs["score"],
                "dbs":       dbs["score"],
            }

    elif direction == "SHORT" and dbs["score"] <= DBS_SHORT_THRESH:
        prob_no = 1.0 - pip_final
        edge    = prob_no - no_price
        if edge >= EDGE_MIN:
            return {
                "side":      "NO",
                "edge":      round(edge, 3),
                "pip":       round(prob_no, 3),
                "mkt_price": no_price,
                "token_id":  cfg.get("token_no", ""),
                "mhs":       mhs["score"],
                "dbs":       dbs["score"],
            }

    return None


# ── Position lifecycle ─────────────────────────────────────────────────────

def open_position(asset: str, opp: dict, capital: float,
                   recent_trades: list, poly_client,
                   state: dict) -> bool:
    """
    Open a new Polymarket position.
    Returns True if the position was successfully recorded.
    """
    price    = opp["mkt_price"]
    usdc     = position_size_usdc(capital, opp["pip"], price, recent_trades)
    if usdc <= 0:
        return False

    shares   = round(usdc / price, 2)
    max_loss = round(shares * price, 2)
    sl, tp   = calc_stop_take(opp["side"], price)

    order_id = ""
    if poly_client and "REEMPLAZAR" not in opp.get("token_id", "REEMPLAZAR"):
        order_id = CLOB.buy(poly_client, opp["token_id"], usdc, price)
        if order_id is None:
            return False  # order failed — don't record the position
    else:
        order_id = "paper"

    pos = PolyPosition(
        asset=asset, side=opp["side"], token_id=opp.get("token_id",""),
        shares=shares, entry_price=price, usdc_spent=max_loss,
        entry_time=datetime.now(ET),
        resolution=WATCH_ASSETS[asset].get("resolution","daily"),
        entry_mhs=opp["mhs"], entry_dbs=opp["dbs"], entry_pip=opp["pip"],
        stop_loss=sl, take_profit=tp, order_id=order_id,
    )

    state["positions"][asset]  = pos
    state["capital_usdc"]     -= max_loss
    state["peak_capital"]      = max(state["peak_capital"],
                                     state["capital_usdc"] + max_loss)

    DB.save_position(asset, pos)
    DB.save_portfolio(state["capital_usdc"], state["peak_capital"])
    return True


def close_position(asset: str, pos: PolyPosition, reason: str,
                    exit_price: float, poly_client, state: dict) -> ClosedTrade:
    """
    Close an open position — sell shares back to the CLOB and record the trade.
    """
    if poly_client and "paper" not in pos.order_id:
        CLOB.sell(poly_client, pos.token_id, pos.shares, exit_price)

    proceeds = round(pos.shares * exit_price, 2)
    pnl      = round(proceeds - pos.usdc_spent, 2)
    duration = str(datetime.now(ET) - pos.entry_time).split(".")[0]
    lr       = log_return(pos.entry_price, max(exit_price, 0.001))
    ev_entry = expected_value(pos.entry_pip, pos.entry_price)

    trade = ClosedTrade(
        asset=asset, side=pos.side,
        entry_price=pos.entry_price, exit_price=exit_price,
        shares=pos.shares, pnl=pnl,
        pnl_pct=round(pnl / pos.usdc_spent * 100, 1),
        log_return=lr, ev_at_entry=round(ev_entry, 4),
        pip_at_entry=pos.entry_pip, duration=duration,
        reason=reason, time=datetime.now(ET).strftime("%H:%M:%S ET"),
    )

    state["trades"].append(trade)
    state["capital_usdc"] += proceeds
    del state["positions"][asset]

    DB.save_trade(trade)
    DB.delete_position(asset)
    DB.save_portfolio(state["capital_usdc"], state["peak_capital"])

    return trade


def monitor_position(asset: str, pos: PolyPosition,
                      cur_clob: Optional[float],
                      mhs_score: float, dbs_score: float,
                      poly_client, state: dict) -> Optional[ClosedTrade]:
    """
    Check if an open position should be closed. Priority order:
      1. Market resolved (CLOB price converged to 0.97+ or 0.03-)
      2. Stop-loss hit
      3. Take-profit hit
      4. Signal reversed (MHS fell or direction flipped)

    Returns the ClosedTrade if closed, None if position remains open.
    """
    if cur_clob is None:
        return None

    # 1. Auto-resolution by CLOB convergence
    if cur_clob >= 0.97:
        return close_position(asset, pos, "WON",  1.0,  poly_client, state)
    if cur_clob <= 0.03:
        return close_position(asset, pos, "LOST", 0.0,  poly_client, state)

    # 2. Stop-loss
    if cur_clob <= pos.stop_loss:
        return close_position(asset, pos, "STOP_LOSS", cur_clob, poly_client, state)

    # 3. Take-profit
    if cur_clob >= pos.take_profit:
        return close_position(asset, pos, "TAKE_PROFIT", cur_clob, poly_client, state)

    # 4. Signal reversal
    signal_reversed = (
        mhs_score < 50 or
        (pos.side == "YES" and dbs_score < -0.3) or
        (pos.side == "NO"  and dbs_score >  0.3)
    )
    if signal_reversed:
        return close_position(asset, pos, "SIGNAL_REVERSED", cur_clob,
                               poly_client, state)

    return None