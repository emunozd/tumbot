"""
data/market_data.py — Market data fetching.

Single Responsibility: fetch price data and persist it.
Three providers, each in its own function:
  - yfinance  → OHLCV candles (1H and 1D)
  - FRED      → macro indicators (VIX, Fed Funds, T10Y, unemployment)
  - Polymarket CLOB → current YES/NO prices for each watched market
"""

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
import requests
import time

from src.config import (
    FRED_KEY, FRED_SERIES, GAMMA_API, POLY_HOST,
    DAILY_TITLE_PATTERNS, DAILY_KEYWORDS, HIST_YEARS, ET
)
from src.models import MacroData
from src.data import database as DB

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


# ══════════════════════════════════════════════════════════════
#  OHLCV — yfinance
# ══════════════════════════════════════════════════════════════

def _df_to_candles(df) -> List[dict]:
    """
    Convert a yfinance DataFrame to candle dicts.
    Handles MultiIndex columns returned by yfinance >= 0.2.x:
      ("Close", "BTC-USD") -> "Close"
    """
    import pandas as pd
    if df is None or df.empty:
        return []

    # Flatten MultiIndex: ("Close", "BTC-USD") -> "Close"
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [col[0] for col in df.columns]

    # yfinance may return "Adj Close" instead of "Close" depending on version
    if "Adj Close" in df.columns and "Close" not in df.columns:
        df = df.rename(columns={"Adj Close": "Close"})

    candles = []
    for ts, row in df.iterrows():
        try:
            candles.append({
                "t": int(ts.timestamp()),
                "o": float(row["Open"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "c": float(row["Close"]),
                "v": float(row.get("Volume", 0) or 0),
            })
        except Exception:
            pass
    return candles


def fetch_1h(ticker: str) -> None:
    """
    Update hourly candles for one ticker.
    Called at HH:01 — fetches only the candle that just closed.

    First run (empty DB): loads 1 month as base.
    Subsequent runs: fetches only the delta since last stored timestamp.
    Skips if DB is already up to date (last candle < 1 hour ago).
    """
    if not HAS_YF:
        return
    try:
        last_ts  = DB.get_last_1h_ts(ticker)
        now_ts   = int(datetime.now(ET).timestamp())
        one_hour = 3600

        if not last_ts:
            df = yf.download(ticker, period="1mo", interval="1h",
                             auto_adjust=True, progress=False, threads=False)
        elif (now_ts - last_ts) < one_hour:
            return   # already up to date
        else:
            since = datetime.fromtimestamp(last_ts - one_hour)
            df = yf.download(ticker, start=since.strftime("%Y-%m-%d %H:00"),
                             interval="1h", auto_adjust=True,
                             progress=False, threads=False)

        candles = _df_to_candles(df)
        if candles:
            DB.save_candles_1h(ticker, candles)
    except Exception:
        pass


def fetch_1d(ticker: str) -> None:
    """
    Update daily candles for one ticker.
    Called at 00:01 ET — fetches only the day that just closed.

    First run (empty DB): loads 6 months as base.
    Subsequent runs: fetches only days since last stored timestamp.
    Also updates candles_hist with the same delta.
    """
    if not HAS_YF:
        return
    try:
        last_ts = DB.get_last_1d_ts(ticker)
        now_ts  = int(datetime.now(ET).timestamp())
        one_day = 86400

        if not last_ts:
            df = yf.download(ticker, period="6mo", interval="1d",
                             auto_adjust=True, progress=False, threads=False)
        elif (now_ts - last_ts) < one_day:
            return   # already up to date
        else:
            since = datetime.fromtimestamp(last_ts - one_day * 2)
            df = yf.download(ticker, start=since.strftime("%Y-%m-%d"),
                             interval="1d", auto_adjust=True,
                             progress=False, threads=False)

        candles = _df_to_candles(df)
        if candles:
            DB.save_candles_1d(ticker, candles)
            DB.save_candles_hist(ticker, candles)
    except Exception:
        pass


def ensure_history(ticker: str, print_fn=print) -> None:
    """
    Ensure the ticker has a full multi-year history in candles_hist.

    First call ever: downloads HIST_YEARS years (slow, ~10s, once only).
    Subsequent calls: downloads only the missing days (fast delta).
    Called once at startup per ticker, then daily at 00:01 ET.
    """
    if not HAS_YF:
        return

    asset  = DB.get_asset(ticker)
    loaded = asset.get("hist_loaded", 0)
    last_ts= DB.get_last_hist_ts(ticker)
    now_ts = int(datetime.now(ET).timestamp())
    one_day= 86400

    if not last_ts or not loaded:
        print_fn(f"[{ticker}] Loading {HIST_YEARS}yr history (first run)...")
        try:
            df = yf.download(ticker, period=f"{HIST_YEARS}y", interval="1d",
                             auto_adjust=True, progress=False, threads=False)
            candles = _df_to_candles(df)
            if candles:
                DB.save_candles_hist(ticker, candles)
                DB.set_hist_loaded(ticker)
                print_fn(f"[{ticker}] {len(candles)} historical candles saved")
        except Exception as e:
            print_fn(f"[{ticker}] History load failed: {e}")

    elif (now_ts - last_ts) > one_day:
        days = (now_ts - last_ts) // one_day
        print_fn(f"[{ticker}] Updating {days} missing day(s)...")
        try:
            since = datetime.fromtimestamp(last_ts - one_day * 2)
            df = yf.download(ticker, start=since.strftime("%Y-%m-%d"),
                             interval="1d", auto_adjust=True,
                             progress=False, threads=False)
            candles = _df_to_candles(df)
            if candles:
                DB.save_candles_hist(ticker, candles)
                print_fn(f"[{ticker}] +{len(candles)} candles added")
        except Exception as e:
            print_fn(f"[{ticker}] History delta failed: {e}")
    else:
        print_fn(f"[{ticker}] History up to date")


# ══════════════════════════════════════════════════════════════
#  MACRO — FRED
# ══════════════════════════════════════════════════════════════

def _fred_latest(series_id: str) -> Tuple[Optional[float], Optional[str]]:
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": FRED_KEY,
                    "file_type": "json", "sort_order": "desc", "limit": "5"},
            timeout=8,
        )
        for obs in r.json().get("observations", []):
            if obs["value"] != ".":
                return float(obs["value"]), obs["date"]
    except Exception:
        pass
    return None, None


def fetch_macro() -> MacroData:
    """
    Fetch all FRED macro indicators.
    Returns a MacroData object (never raises — returns defaults on failure).
    """
    raw: Dict[str, Optional[float]] = {}
    for name, sid in FRED_SERIES.items():
        val, _ = _fred_latest(sid)
        raw[name] = val
        time.sleep(0.15)   # polite rate limiting

    vix = raw.get("VIX")
    fed = raw.get("FED_RATE")
    t10 = raw.get("T10Y")

    return MacroData(
        vix=vix,
        fed_rate=fed,
        t10y=t10,
        unrate=raw.get("UNRATE"),
        spread=round(t10 - fed, 4) if t10 and fed else None,
    )


# ══════════════════════════════════════════════════════════════
#  POLYMARKET — prices and market discovery
# ══════════════════════════════════════════════════════════════

def fetch_poly_price(token_yes: str, poly_client=None) -> Dict[str, Optional[float]]:
    """
    Fetch YES/NO midpoint prices for a single market.
    Uses the authenticated CLOB client if available, falls back to public REST.
    """
    yes_price = None

    if poly_client:
        try:
            mid_data  = poly_client.get_midpoint(token_yes)
            yes_price = float(mid_data.get("mid", 0.5))
        except Exception:
            pass

    if yes_price is None:
        try:
            r = requests.get(
                f"{POLY_HOST}/midpoints",
                params={"token_ids": token_yes},
                timeout=5,
            )
            data = r.json()
            if isinstance(data, list) and data:
                yes_price = float(data[0].get("mid", 0.5))
        except Exception:
            pass

    if yes_price is None:
        return {"yes": None, "no": None, "mid": None}

    yes_price = round(yes_price, 3)
    return {"yes": yes_price, "no": round(1.0 - yes_price, 3), "mid": yes_price}


def _extract_tokens(market: dict) -> Dict[str, str]:
    tokens = market.get("clobTokenIds", [])
    if len(tokens) >= 2:
        return {"yes": tokens[0], "no": tokens[1]}
    outcomes = market.get("outcomes", [])
    if len(outcomes) >= 2:
        yes = outcomes[0].get("clobTokenId", "")
        no  = outcomes[1].get("clobTokenId", "")
        if yes and no:
            return {"yes": yes, "no": no}
    return {}


# Price-movement verbs that indicate a daily Up/Down market
_PRICE_VERBS = (
    "go up", "go down", "price up", "price down",
    "higher than", "lower than", "above", "below",
    "close above", "close below", "end above", "end below",
    "up today", "down today", "up on", "down on",
)

# Tags that disqualify a market even if it mentions the asset name.
# Catches things like "MegaETH market cap", "ETH ETF", "ETH staking yield", etc.
_DISQUALIFY = (
    "market cap", "mcap", "fDV", "tvl", "etf",
    "staking", "yield", "dominance", "supply",
    "launch", "listing", "airdrop", "funding",
    "fees", "revenue", "holders",
)


def _is_daily_price_market(m: dict, asset_keys: list) -> bool:
    """
    Returns True only if the market is a daily Up/Down price market
    for the given asset.

    Requires ALL of:
      1. Question mentions the asset (btc / bitcoin / eth / ethereum / qqq)
      2. Question contains a price-movement verb (go up, above, higher than…)
      3. Question does NOT contain disqualifying terms (market cap, etf, etc.)
      4. endDate is within the next 2 days (daily resolution)
    """
    q = m.get("question", "").lower()

    # Must mention the asset
    if not any(k in q for k in asset_keys):
        return False

    # Must contain a price-movement verb
    if not any(v in q for v in _PRICE_VERBS):
        return False

    # Must NOT contain disqualifying terms
    if any(d in q for d in _DISQUALIFY):
        return False

    # Must resolve within 2 days
    end_date = m.get("endDate", "") or m.get("end_date", "")
    if end_date:
        try:
            ed = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days_left = (ed - datetime.now(ed.tzinfo)).days
            return days_left <= 2
        except Exception:
            pass

    return False


def _make_daily_slug(prefix: str, dt: datetime) -> str:
    """
    Build the Polymarket event slug for a given date.
    Pattern: {prefix}-{month}-{day}-{year}
    Example: bitcoin-up-or-down-on-march-23-2026
    """
    month = dt.strftime("%B").lower()   # "march"  (no leading zero)
    day   = str(dt.day)                 # "23"     (no leading zero)
    year  = str(dt.year)                # "2026"
    return f"{prefix}-{month}-{day}-{year}"


def discover_daily_markets(watch_assets: dict) -> Dict[str, dict]:
    """
    Discover today's active daily markets for each watched asset.

    Strategy cascade — stops as soon as a valid market is found:
      1. Date-based slug for TODAY   (deterministic, most reliable)
         e.g. bitcoin-up-or-down-on-march-23-2026
      2. Date-based slug for TOMORROW (markets sometimes open a day early)
      3. Keyword + price-verb filter over all active markets (fallback)

    Updates watch_assets in-place and persists token IDs to the DB.
    Returns a dict of {ticker: market_info} for found markets.
    """
    found: Dict[str, dict] = {}
    now      = datetime.now(ET)
    tomorrow = now + timedelta(days=1)

    for ticker, cfg in watch_assets.items():
        prefix = cfg.get("poly_slug_prefix", "")
        keys   = DAILY_TITLE_PATTERNS.get(ticker, [ticker.replace("-USD","").lower()])
        market = None

        # Strategy 1 & 2: deterministic date-based slug
        for dt in [now, tomorrow]:
            if market:
                break
            if not prefix:
                break
            slug = _make_daily_slug(prefix, dt)
            # Update config so logs show the attempted slug
            watch_assets[ticker]["poly_slug"] = slug
            try:
                r = requests.get(f"{GAMMA_API}/markets",
                                 params={"slug": slug},
                                 timeout=6)
                data = r.json()
                if data:
                    candidate = data[0]
                    # Verify the slug matches exactly — Gamma API can return
                    # related markets if the slug doesn't exist
                    returned_slug = candidate.get("slug", "")
                    if returned_slug == slug:
                        market = candidate
            except Exception:
                pass

        # Strategy 3: keyword + price-verb filter over active markets
        if not market:
            try:
                r = requests.get(f"{GAMMA_API}/markets",
                                 params={"active": True, "closed": False,
                                         "limit": 200},
                                 timeout=8)
                for m in r.json():
                    if _is_daily_price_market(m, keys):
                        market = m
                        break
            except Exception:
                pass

        if market:
            tokens = _extract_tokens(market)
            if tokens:
                info = {
                    "token_yes":    tokens["yes"],
                    "token_no":     tokens["no"],
                    "question":     market.get("question", ""),
                    "end_date":     market.get("endDate", ""),
                    "condition_id": market.get("conditionId", ""),
                }
                found[ticker] = info
                watch_assets[ticker]["token_yes"] = tokens["yes"]
                watch_assets[ticker]["token_no"]  = tokens["no"]
                DB.update_asset_tokens(
                    ticker, tokens["yes"], tokens["no"],
                    info["question"], info["condition_id"]
                )

    return found