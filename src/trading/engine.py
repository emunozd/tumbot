"""
trading/engine.py — Signal evaluation and position lifecycle.

Single Responsibility: decide when to open and close positions.

Entry logic updated to use DirectionalPredictor (D+1 price direction)
instead of MHS threshold. MHS/DBS are kept for monitoring/display.

New entry conditions:
  1. DirectionalScore magnitude >= DIRECTIONAL_MIN (45 default)
  2. Polymarket YES price between 0.30 and POLY_PRICE_MAX (0.65)
     — ensures crowd hasn't already priced the move
  3. Current ET hour between ENTRY_HOUR_START (4:30) and ENTRY_HOUR_END (9:00)
     — early window when market is least efficient
  4. VIX not in panic territory (< VIX_BLOCK)
  5. Edge (PIP vs market price) >= EDGE_MIN
"""

import math
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from src.config import (
    MHS_MIN_DAILY, MHS_MIN_WEEKLY, DBS_LONG_THRESH, DBS_SHORT_THRESH,
    EDGE_MIN, ENTRY_WINDOW_START, ENTRY_WINDOW_END, WATCH_ASSETS, ET,
    DIRECTIONAL_MIN, POLY_PRICE_MAX, TIME_OFFSET, TIME_OFFSET_WINDOW,
    VIX_BLOCK,
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
    Check if an asset has candle data available (basic prerequisite).
    Time-based entry restriction is handled separately in detect_opportunity().
    """
    cfg        = WATCH_ASSETS.get(asset, {})\

    if cfg.get("asset_type") == "crypto":
        return True

    last_ts = DB.get_last_1d_ts(asset)
    return bool(last_ts and last_ts > 0)


def _in_entry_time_window(asset: str) -> bool:
    """
    Returns True if current ET time is within the configured entry window
    for this asset's Polymarket daily market.

    Window logic:
      market_ref   = resolve_dt - 24h   (when market was freshest, ~0.50 price)
      window_start = market_ref + TIME_OFFSET hours
      window_end   = window_start + TIME_OFFSET_WINDOW hours

    Example with defaults (OFFSET=6, WINDOW=10):
      Crypto (noon resolution):  yesterday 18:00 → today 04:00 ET
      Equity (4PM resolution):   yesterday 22:00 → today 08:00 ET

    The comparison is done in absolute datetime (not mod 24) so crossing
    midnight is handled correctly — 05:00 today IS after 22:00 yesterday.
    """
    from datetime import timedelta
    cfg           = WATCH_ASSETS.get(asset, {})
    resolves_hour = cfg.get("resolves_hour", 12)
    resolves_min  = cfg.get("resolves_minute", 0)

    now        = datetime.now(ET)
    resolve_dt = now.replace(
        hour=resolves_hour, minute=resolves_min, second=0, microsecond=0
    )
    # If today's resolution already passed, next one is tomorrow
    if resolve_dt <= now:
        resolve_dt += timedelta(days=1)

    # Market reference = 24h before resolution (when price was fresh ~0.50)
    market_ref   = resolve_dt - timedelta(hours=24)
    window_start = market_ref + timedelta(hours=TIME_OFFSET)
    window_end   = window_start + timedelta(hours=TIME_OFFSET_WINDOW)

    return window_start <= now <= window_end


# ── Opportunity detection ──────────────────────────────────────────────────

def detect_opportunity(
    asset: str,
    mhs: dict,
    dbs: dict,
    pip_final: float,
    poly_prices: dict,
    directional: dict,
    macro: MacroData,
) -> Optional[dict]:
    """
    Determine if there is a tradeable opportunity using the DirectionalPredictor.

    Entry requires ALL of:
      1. DirectionalScore magnitude >= DIRECTIONAL_MIN (conviction threshold)
      2. Direction is UP or DOWN (not NEUTRAL)
      3. VIX below panic threshold
      4. Market price not already reflecting the move (YES <= POLY_PRICE_MAX)
      5. Current time within early entry window (4:30–9:00 AM ET)
      6. Candle data available for this asset
      7. Edge (PIP vs market price) >= EDGE_MIN after LLM adjustment

    MHS is no longer a hard gate — it's still computed and shown in the UI
    but does not block entry. The DirectionalScore replaces it as the gate.
    """
    # 1. Candle data prerequisite
    if not in_entry_window(asset):
        return None

    # 2. Already traded this asset in the current resolution window?
    #    lookback = 24h - hours_remaining_to_resolution
    #    Prevents re-entering a market already traded in the same prediction window.
    cfg_asset     = WATCH_ASSETS.get(asset, {})
    resolves_hour = cfg_asset.get("resolves_hour", 12)
    resolves_min  = cfg_asset.get("resolves_minute", 0)
    now_et        = datetime.now(ET)
    from datetime import timedelta as _td
    resolve_dt    = now_et.replace(
        hour=resolves_hour, minute=resolves_min, second=0, microsecond=0
    )
    if resolve_dt <= now_et:          # already resolved today — use tomorrow's window
        resolve_dt += _td(days=1)
    hours_remaining = (resolve_dt - now_et).total_seconds() / 3600
    lookback_hours  = max(1.0, 24.0 - hours_remaining)
    if DB.was_traded_recently(asset, lookback_hours):
        return None   # already traded this asset in the current window

    # 3. DirectionalScore conviction
    d_score = directional.get("score", 0)
    d_dir   = directional.get("direction", "NEUTRAL")
    if abs(d_score) < DIRECTIONAL_MIN:
        return None
    if d_dir == "NEUTRAL":
        return None

    # 3. VIX panic block — kept even in new system
    if mhs.get("blocked"):
        return None

    # 4. Time window — only enter in early morning window
    if not _in_entry_time_window(asset):
        return None

    yes_price = poly_prices.get("yes")
    no_price  = poly_prices.get("no")
    if yes_price is None:
        return None

    cfg = WATCH_ASSETS.get(asset, {})

    # 5. Long (UP) entry
    if d_dir == "UP":
        # Market must not have already priced the move
        if yes_price > POLY_PRICE_MAX:
            return None
        edge = pip_final - yes_price
        if edge >= EDGE_MIN:
            return {
                "side":             "YES",
                "edge":             round(edge, 3),
                "pip":              pip_final,
                "mkt_price":        yes_price,
                "token_id":         cfg.get("token_yes", ""),
                "mhs":              mhs.get("score", 0),
                "dbs":              dbs.get("score", 0),
                "directional":      d_score,
                "conviction":       directional.get("conviction", "LOW"),
            }

    # 6. Short (DOWN) entry
    elif d_dir == "DOWN":
        if no_price is None:
            return None
        # For NO trades: market prices DOWN move → NO price is high
        # We check no_price <= POLY_PRICE_MAX (equivalent check on NO side)
        if no_price > POLY_PRICE_MAX:
            return None
        prob_no = 1.0 - pip_final
        edge    = prob_no - no_price
        if edge >= EDGE_MIN:
            return {
                "side":             "NO",
                "edge":             round(edge, 3),
                "pip":              round(prob_no, 3),
                "mkt_price":        no_price,
                "token_id":         cfg.get("token_no", ""),
                "mhs":              mhs.get("score", 0),
                "dbs":              dbs.get("score", 0),
                "directional":      d_score,
                "conviction":       directional.get("conviction", "LOW"),
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
            return False
    else:
        order_id = "paper"

    pos = PolyPosition(
        asset=asset, side=opp["side"], token_id=opp.get("token_id", ""),
        shares=shares, entry_price=price, usdc_spent=max_loss,
        entry_time=datetime.now(ET),
        resolution=WATCH_ASSETS[asset].get("resolution", "daily"),
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
      1. Market resolved (CLOB price converged to ≥0.97 or ≤0.03)
      2. Stop-loss hit
      3. Take-profit hit
      4. Signal reversed (directional score flipped strongly)
    """
    if cur_clob is None:
        return None

    # 1. Auto-resolution
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

    # 4. Signal reversal — use directional score if available, else MHS/DBS
    directional = state.get("directional_score", {}).get(asset, {})
    d_dir = directional.get("direction", "NEUTRAL")

    signal_reversed = False
    if d_dir != "NEUTRAL":
        # Directional predictor flipped against our position
        if pos.side == "YES" and d_dir == "DOWN":
            signal_reversed = True
        elif pos.side == "NO" and d_dir == "UP":
            signal_reversed = True
    else:
        # Fallback to MHS/DBS check
        signal_reversed = (
            mhs_score < 40 or
            (pos.side == "YES" and dbs_score < -0.4) or
            (pos.side == "NO"  and dbs_score >  0.4)
        )

    if signal_reversed:
        return close_position(asset, pos, "SIGNAL_REVERSED", cur_clob,
                               poly_client, state)

    return None