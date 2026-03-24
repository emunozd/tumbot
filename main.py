#!/usr/bin/env python3
"""
main.py — Entry point for the tumbot — Polymarket trading bot.

This file is intentionally thin. It:
  1. Initialises shared state and dependencies
  2. Runs the startup sequence (DB, history, market discovery)
  3. Drives the main clock loop (no business logic here)

All actual logic lives in the src/* modules.
"""

import sys
import time
import logging
import threading
from datetime import datetime
from dataclasses import fields as dc_fields

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("tumbot.telegram").setLevel(logging.DEBUG)

from src.config import (
    WATCH_ASSETS, FRED_KEY, POLY_PK, POLY_FUNDER, CAPITAL_INITIAL,
    POLY_HOST, POLY_CHAIN, ET, LLM_BACKEND, LLM_MODEL
)
from src.data import database as DB
from src.data.market_data import (
    fetch_1h, fetch_1d, ensure_history,
    fetch_poly_price, fetch_macro, discover_daily_markets
)
from src.llm.analysis import analyze_sentiment
from src.llm.client import is_available as llm_available
from src.signals.scoring import compute_mhs, compute_dbs, compute_pip, daily_trend
from src.signals.stats import bootstrap_win_rate_ci
from src.llm.analysis import validate_pip
from src.signals.indicators import expected_value, expected_log_return
from src.trading.engine import detect_opportunity, open_position, monitor_position
from src.models import PolyPosition, SentimentData, MacroData
from src.ui.display import build_layout, console
from src.telegram import bot as tg_bot

try:
    import finnhub as _fh
    HAS_FH = bool(__import__("os").environ.get("FINNHUB_API_KEY"))
    fh_client = _fh.Client(api_key=__import__("os").environ.get("FINNHUB_API_KEY","")) \
                if HAS_FH else None
except ImportError:
    fh_client = None

try:
    from py_clob_client.client import ClobClient
    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False

from rich.live import Live
from rich.panel import Panel

# ── Validate required env vars ─────────────────────────────────────────────
if not FRED_KEY:
    print("\n⚠  FRED_API_KEY is required. Register free at fred.stlouisfed.org\n")
    sys.exit(1)

try:
    import yfinance  # noqa
except ImportError:
    print("\n⚠  yfinance not installed: pip install yfinance\n")
    sys.exit(1)

# ── Live trading client ────────────────────────────────────────────────────
LIVE_MODE  = bool(POLY_PK and POLY_FUNDER and HAS_CLOB)
poly_client = None

if LIVE_MODE:
    try:
        poly_client = ClobClient(
            POLY_HOST, key=POLY_PK, chain_id=POLY_CHAIN,
            signature_type=1, funder=POLY_FUNDER,
        )
        poly_client.set_api_creds(poly_client.create_or_derive_api_creds())
        console.print("[green]✓ Connected to Polymarket CLOB (LIVE mode)[/]")
    except Exception as e:
        console.print(f"[red]⚠ Polymarket CLOB connection failed: {e}[/]")
        LIVE_MODE = False

# ── Shared state ───────────────────────────────────────────────────────────
lock = threading.Lock()

# Throttle: validate_pip is called at most once every 30 min per asset.
# The LLM has no new intraday information between consecutive 1-min ticks,
# so calling it every minute wastes tokens without improving signal quality.
PIP_THROTTLE_SECS = 1800  # 30 minutes — aligned with sentiment refresh cadence

state = {
    "candles_1h":          {a: [] for a in WATCH_ASSETS},
    "candles_1d":          {a: [] for a in WATCH_ASSETS},
    "last_price":          {a: None for a in WATCH_ASSETS},
    "mhs":                 {a: {} for a in WATCH_ASSETS},
    "dbs":                 {a: {} for a in WATCH_ASSETS},
    "pip":                 {a: None for a in WATCH_ASSETS},
    "pip_validated":       {a: {} for a in WATCH_ASSETS},
    "ev":                  {a: None for a in WATCH_ASSETS},
    "elr":                 {a: None for a in WATCH_ASSETS},
    "tf_trend":            {a: "neutral" for a in WATCH_ASSETS},
    "poly_prices":         {a: {"yes": None, "no": None, "mid": None} for a in WATCH_ASSETS},
    "opportunities":       {a: None for a in WATCH_ASSETS},
    "positions":           {},
    "trades":              [],
    "capital_usdc":        CAPITAL_INITIAL,
    "peak_capital":        CAPITAL_INITIAL,
    "bootstrap_ci":        None,   # (lo, hi) tuple once 30+ trades exist
    "macro_data":          None,
    "sentiment_data":      None,
    "news":                [],
    "last_update":         "—",
    "last_signal":         "—",
    "status":              "Starting...",
    "fetching":            set(),
    "countdown":           60,
    # ── LLM throttle ──────────────────────────────────────────────────────
    # Tracks the last time validate_pip was actually called per asset.
    # Key: asset ticker  Value: time.time() float
    "last_pip_validation": {},
}


# ── Background task runner ─────────────────────────────────────────────────

def _run(fn):
    def wrapper():
        try:
            fn()
        except Exception as e:
            with lock:
                state["status"] = f"⚠ {fn.__name__}: {str(e)[:55]}"
    return wrapper

def bg(fn):
    threading.Thread(target=_run(fn), daemon=True).start()


# ── Fetch functions ────────────────────────────────────────────────────────

def _reload_candles():
    for asset in WATCH_ASSETS:
        c1h = DB.load_candles_1h(asset, limit=100)
        c1d = DB.load_candles_1d(asset, limit=100)
        with lock:
            state["candles_1h"][asset] = c1h
            state["candles_1d"][asset] = c1d
            if c1h:
                state["last_price"][asset] = c1h[-1]["c"]
    with lock:
        state["last_update"] = datetime.now(ET).strftime("%H:%M:%S ET")


def do_fetch_1h():
    with lock: state["fetching"].add("candles_1h")
    try:
        for asset in WATCH_ASSETS:
            fetch_1h(asset)
        _reload_candles()
    finally:
        with lock: state["fetching"].discard("candles_1h")


def do_fetch_1d():
    with lock: state["fetching"].add("candles_1d")
    try:
        for asset in WATCH_ASSETS:
            fetch_1d(asset)
            ensure_history(asset)
        _reload_candles()
    finally:
        with lock: state["fetching"].discard("candles_1d")


def do_fetch_macro():
    with lock: state["fetching"].add("macro")
    try:
        macro = fetch_macro()
        with lock:
            state["macro_data"] = macro
    finally:
        with lock: state["fetching"].discard("macro")


def do_fetch_news_sentiment():
    with lock: state["fetching"].add("sentiment")
    try:
        headlines = []
        if fh_client:
            general = fh_client.general_news("general", min_id=0)
            for n in (general or [])[:15]:
                headlines.append({"headline": n.get("headline",""),
                                   "source": n.get("source","")})
        with lock:
            state["news"] = headlines

        sent = analyze_sentiment(headlines)
        with lock:
            state["sentiment_data"] = sent
    finally:
        with lock: state["fetching"].discard("sentiment")


def do_fetch_prices():
    with lock: state["fetching"].add("prices")
    try:
        for asset, cfg in WATCH_ASSETS.items():
            token_yes = cfg.get("token_yes","")
            if not token_yes or "REEMPLAZAR" in token_yes:
                continue
            prices = fetch_poly_price(token_yes, poly_client)
            with lock:
                state["poly_prices"][asset] = prices
    finally:
        with lock: state["fetching"].discard("prices")


def do_run_signals():
    with lock: state["fetching"].add("signal")
    try:
        macro = state.get("macro_data") or MacroData()
        sent  = state.get("sentiment_data") or SentimentData()
        recent_trades = DB.load_recent_trades(limit=200)
        now_ts = time.time()

        for asset in WATCH_ASSETS:
            c1h = state["candles_1h"].get(asset, [])
            c1d = state["candles_1d"].get(asset, [])

            if not c1h:
                continue

            mhs_data = compute_mhs(c1h, sent, macro)
            dbs_data = compute_dbs(c1h, c1d, macro, sent,
                                    day_change=state["poly_prices"][asset].get("mid",0) or 0)
            tf       = daily_trend(c1d)
            pip_raw  = compute_pip(dbs_data["score"])

            # ── Bayesian PIP validation (throttled to once per 30 min per asset) ──
            # The LLM inputs (VIX, sentiment, macro) only refresh every 30-60 min,
            # so calling validate_pip every minute adds cost without new signal value.
            # On the first call ever (no prior timestamp) it runs immediately.
            pip_validated = state["pip_validated"].get(asset, {})
            pip_final     = pip_raw

            if mhs_data["score"] >= 70 and not mhs_data["blocked"]:
                last_val_ts = state["last_pip_validation"].get(asset, 0)
                if (now_ts - last_val_ts) >= PIP_THROTTLE_SECS:
                    # Throttle window elapsed — call the LLM and update timestamp
                    pip_validated = validate_pip(
                        asset, pip_raw, mhs_data["score"],
                        dbs_data["score"], tf, macro, sent
                    )
                    with lock:
                        state["last_pip_validation"][asset] = now_ts
                # Always apply whatever validated PIP we have (fresh or cached)
                if pip_validated.get("valid", True):
                    pip_final = pip_validated.get("adjusted_pip", pip_raw)

            pp      = state["poly_prices"].get(asset, {})
            side_p  = pp.get("yes") if dbs_data["direction"]=="LONG" else pp.get("no")
            ev_val  = expected_value(pip_final, side_p)  if side_p else None
            elr_val = expected_log_return(pip_final, side_p) if side_p else None

            opp = detect_opportunity(asset, mhs_data, dbs_data, pip_final, pp)
            # Recalculate edge with validated PIP
            if opp and pip_validated:
                opp["pip"]  = pip_final
                opp["edge"] = round(pip_final - opp["mkt_price"], 3)
                if opp["edge"] < 0.08:
                    opp = None

            with lock:
                state["mhs"][asset]           = mhs_data
                state["dbs"][asset]           = dbs_data
                state["tf_trend"][asset]      = tf
                state["pip"][asset]           = pip_raw
                state["pip_validated"][asset] = pip_validated
                state["ev"][asset]            = ev_val
                state["elr"][asset]           = elr_val
                state["opportunities"][asset] = opp
                state["last_signal"]          = datetime.now(ET).strftime("%H:%M:%S ET")

            # Monitor open position
            pos = state["positions"].get(asset)
            if pos and pos.status == "OPEN":
                cur_clob = pp.get("yes") if pos.side=="YES" else pp.get("no")
                trade = monitor_position(
                    asset, pos, cur_clob,
                    mhs_data["score"], dbs_data["score"],
                    poly_client, state
                )
                if trade:
                    with lock:
                        state["last_signal"] = datetime.now(ET).strftime("%H:%M:%S ET")
                    continue

            # Open new position
            if opp and asset not in state["positions"]:
                opened = open_position(
                    asset, opp, state["capital_usdc"],
                    recent_trades, poly_client, state
                )
                if opened:
                    with lock:
                        state["last_signal"] = datetime.now(ET).strftime("%H:%M:%S ET")

        # ── Bootstrap CI — recompute only when trade count changes ─────────
        # 10K iterations take ~0.1s; skip if nothing new to avoid CPU waste
        current_count = len(recent_trades)
        if current_count != state.get("_last_bootstrap_n", -1):
            ci = bootstrap_win_rate_ci(recent_trades)
            with lock:
                state["bootstrap_ci"]      = ci
                state["_last_bootstrap_n"] = current_count

    finally:
        with lock: state["fetching"].discard("signal")


# ── Startup ────────────────────────────────────────────────────────────────

def startup():
    console.print(Panel.fit(
        "[bold cyan]tumbot[/]\n"
        "[dim]yfinance 1H+1D · FRED macro · LLM-agnostic · SQLite[/]\n"
        "[dim]MHS · DBS · PIP · Bayesian validation · Kelly · CLOB retry[/]\n"
        f"[dim]Mode: {'LIVE (Polymarket CLOB)' if LIVE_MODE else 'PAPER (no real orders)'}[/]\n"
        f"[dim]LLM:  {LLM_BACKEND}/{LLM_MODEL.split('/')[-1]}  "
        f"({'available' if llm_available() else 'unavailable'})[/]",
        border_style="cyan",
    ))

    if not LIVE_MODE:
        console.print(
            "[dim]To trade live set:\n"
            "  POLY_PRIVATE_KEY   — Polygon wallet private key\n"
            "  POLY_FUNDER_ADDRESS — wallet address holding USDC\n"
            "  pip install py-clob-client[/]\n"
        )

    # Database setup
    DB.init_db()
    for ticker, cfg in WATCH_ASSETS.items():
        DB.upsert_asset(ticker, cfg["name"], cfg["asset_type"], cfg["poly_slug"])

    # Restore known tokens from DB into WATCH_ASSETS in-memory dict.
    # Without this, do_fetch_prices() skips assets whose token_yes is ""
    # even when a valid token was discovered and persisted in a prior session.
    for ticker in WATCH_ASSETS:
        row = DB.get_asset(ticker)
        if row.get("token_yes"):
            WATCH_ASSETS[ticker]["token_yes"] = row["token_yes"]
            WATCH_ASSETS[ticker]["token_no"]  = row.get("token_no", "")
            console.print(f"[dim]  {ticker}: token restaurado de DB[/]")

    # Recover state from previous session
    portfolio = DB.load_portfolio()
    if portfolio:
        state["capital_usdc"] = portfolio["capital_usdc"]
        state["peak_capital"] = portfolio["peak_capital"]
        console.print(f"[dim]Capital restored: ${portfolio['capital_usdc']:.2f} USDC[/]")
    else:
        DB.save_portfolio(state["capital_usdc"], state["peak_capital"])

    open_pos = DB.load_open_positions()
    if open_pos:
        console.print(f"[yellow]Recovering {len(open_pos)} open position(s)...[/]")
        valid_fields = {f.name for f in dc_fields(PolyPosition)}
        for asset, p in open_pos.items():
            try:
                p["entry_time"] = datetime.fromisoformat(p["entry_time"])
                state["positions"][asset] = PolyPosition(
                    **{k: v for k, v in p.items() if k in valid_fields}
                )
                state["capital_usdc"] -= p["usdc_spent"]
                console.print(f"[dim]  {asset}: {p['side']} ${p['usdc_spent']:.2f}[/]")
            except Exception as e:
                console.print(f"[red]  Could not restore {asset}: {e}[/]")

    # Load price history
    console.print("[dim]Checking price history...[/]")
    for ticker in WATCH_ASSETS:
        ensure_history(ticker, print_fn=lambda m: console.print(f"[dim]{m}[/]"))

    # Discover today's Polymarket markets
    console.print("[dim]Discovering today's markets...[/]")
    found = discover_daily_markets(WATCH_ASSETS)
    if found:
        for asset, info in found.items():
            console.print(f"[green]✓ {asset}: {info['question'][:60]}[/]")
    else:
        console.print("[yellow]⚠ No markets found automatically. "
                      "Set token_yes/token_no manually in config.py[/]")

    # Initial data load
    threads = [
        threading.Thread(target=_run(do_fetch_1h),  daemon=True),
        threading.Thread(target=_run(do_fetch_macro), daemon=True),
        threading.Thread(target=_run(do_fetch_news_sentiment), daemon=True),
    ]
    for t in threads: t.start()
    for t in threads: t.join(timeout=45)

    do_fetch_prices()
    do_run_signals()


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    startup()
    tg_bot.init(state, lock)

    # Clock-based trigger flags
    _last_1h_hour  = -1
    _last_1d_date  = None
    _last_macro_h  = -1
    _last_news_min = -1
    _last_sig_min  = -1
    _last_price_ts = 0.0
    _last_market_h = -1

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while True:
            now = datetime.now(ET)
            with lock:
                state["countdown"] = 60 - now.second

            live.update(build_layout(state, LIVE_MODE))
            time.sleep(0.5)

            # ── Polymarket prices: every 30 seconds ──────────────────
            if time.time() - _last_price_ts >= 30:
                _last_price_ts = time.time()
                bg(do_fetch_prices)

            # ── Hourly candles: at HH:01 ─────────────────────────────
            if now.minute == 1 and now.hour != _last_1h_hour:
                _last_1h_hour = now.hour
                bg(do_fetch_1h)

            # ── Daily candles + history delta: at 00:01 ET ───────────
            if now.hour == 0 and now.minute == 1 and now.date() != _last_1d_date:
                _last_1d_date = now.date()
                bg(do_fetch_1d)

            # ── Macro FRED: once per hour at HH:02 ───────────────────
            if now.minute == 2 and now.hour != _last_macro_h:
                _last_macro_h = now.hour
                bg(do_fetch_macro)

            # ── News + NLP: every 30 minutes at HH:00 and HH:30 ──────
            if now.minute in (0, 30) and now.minute != _last_news_min:
                _last_news_min = now.minute
                bg(do_fetch_news_sentiment)

            # ── Signal engine: every minute ───────────────────────────
            if now.minute != _last_sig_min:
                _last_sig_min = now.minute
                bg(do_run_signals)
                with lock:
                    opps = [a for a, o in state["opportunities"].items() if o]
                    n_pos = len(state["positions"])
                    if opps:
                        state["status"] = f"🔍 Edge detectado: {', '.join(opps)}"
                    elif n_pos:
                        state["status"] = f"📊 Monitoreando {n_pos} posición(es)"
                    else:
                        mhs_vals = [v.get('score',0) for v in state['mhs'].values() if isinstance(v, dict)]
                        avg_mhs = sum(mhs_vals)/len(mhs_vals) if mhs_vals else 0
                        state["status"] = f"👁 Escaneando — MHS avg {avg_mhs:.0f}/100"

            # ── Market refresh: hourly at HH:03, fast near resolution ──
            # Normal cadence: every hour at HH:03.
            # Fast cadence: every 5 min in window [resolution-5min, resolution+60min]
            # so the bot switches to the next day's market as soon as it's live.
            def _in_resolution_window(now_dt) -> bool:
                """True if any watched asset is within its resolution window."""
                for cfg in WATCH_ASSETS.values():
                    rh = cfg.get("resolves_hour", -1)
                    rm = cfg.get("resolves_minute", 0)
                    res = now_dt.replace(hour=rh, minute=rm, second=0, microsecond=0)
                    diff = (now_dt - res).total_seconds()
                    if -300 <= diff <= 3600:   # 5 min before → 60 min after
                        return True
                return False

            in_res_window = _in_resolution_window(now)

            run_discovery = False
            if in_res_window:
                # Fast mode: every 5 minutes
                if now.minute % 5 == 0 and now.minute != _last_market_h:
                    _last_market_h = now.minute
                    run_discovery  = True
            else:
                # Normal mode: once per hour at HH:03
                if now.minute == 3 and now.hour != _last_market_h:
                    _last_market_h = now.hour
                    run_discovery  = True

            if run_discovery:
                def _do_discovery():
                    from src.trading.engine import close_position
                    import src.telegram.bot as _tg

                    # Snapshot token_ids before discovery
                    old_tokens = {
                        a: WATCH_ASSETS[a].get("token_yes", "")
                        for a in WATCH_ASSETS
                    }

                    found = discover_daily_markets(WATCH_ASSETS)

                    # Detect market switch — close orphaned positions
                    for asset in WATCH_ASSETS:
                        new_token = WATCH_ASSETS[asset].get("token_yes", "")
                        if not new_token or new_token == old_tokens[asset]:
                            continue  # no change

                        with lock:
                            pos = state["positions"].get(asset)

                        if pos:
                            # Market switched — position is orphaned
                            # Use last known price or fall back to entry price
                            last_p = state["poly_prices"].get(asset, {})
                            exit_p = (
                                last_p.get("yes") if pos.side == "YES"
                                else last_p.get("no")
                            ) or pos.entry_price

                            with lock:
                                trade = close_position(
                                    asset, pos, "MARKET_EXPIRED",
                                    exit_p, poly_client, state
                                )
                            console.print(
                                f"[yellow]⚠ {asset}: mercado expiró con posición abierta "
                                f"→ cerrada como MARKET_EXPIRED "
                                f"PnL=${trade.pnl:+.2f}[/]"
                            )
                            _tg.alert_position_closed(
                                asset, pos.side, "MARKET_EXPIRED",
                                trade.pnl, trade.pnl_pct,
                                pos.entry_price, exit_p,
                            )

                    if now.hour == 16:
                        today    = now.strftime("%Y-%m-%d")
                        recent   = DB.load_recent_trades(limit=100)
                        today_ts = [t for t in recent if t["time"][:10] == today]
                        DB.save_daily_snapshot(state["capital_usdc"], today_ts)

                bg(_do_discovery)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped. Goodbye.[/]")