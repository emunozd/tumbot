"""
config.py — Single source of truth for all bot configuration.

All values can be overridden via environment variables.
No logic here — only constants and typed settings.
"""

import os
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Dict, Tuple

ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# ── Watched assets ─────────────────────────────────────────────────────────
WATCH_ASSETS: Dict[str, dict] = {
    "BTC-USD": {
        "name":             "Bitcoin",
        "poly_slug_prefix": "bitcoin-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "crypto",
        "resolves_hour":    12,
        "resolves_minute":  0,
    },
    "ETH-USD": {
        "name":             "Ethereum",
        "poly_slug_prefix": "ethereum-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "crypto",
        "resolves_hour":    12,
        "resolves_minute":  0,
    },
    "XRP-USD": {
        "name":             "XRP",
        "poly_slug_prefix": "xrp-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "crypto",
        "resolves_hour":    12,
        "resolves_minute":  0,
    },
    "^GSPC": {
        "name":             "S&P 500",
        "poly_slug_prefix": "spx-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "equity_etf",
        "resolves_hour":    16,
        "resolves_minute":  0,
    },
    "^NDX": {
        "name":             "Nasdaq 100",
        "poly_slug_prefix": "ndx-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "equity_etf",
        "resolves_hour":    16,
        "resolves_minute":  0,
    },
    "AAPL": {
        "name":             "Apple",
        "poly_slug_prefix": "aapl-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "equity_etf",
        "resolves_hour":    16,
        "resolves_minute":  0,
    },
}

# ── Signal thresholds (MHS/DBS — kept for display and monitoring) ──────────
MHS_MIN_DAILY   = float(os.environ.get("MHS_MIN_DAILY",  "75"))
MHS_MIN_WEEKLY  = float(os.environ.get("MHS_MIN_WEEKLY", "82"))
DBS_LONG_THRESH = float(os.environ.get("DBS_LONG",  "0.50"))
DBS_SHORT_THRESH= float(os.environ.get("DBS_SHORT", "-0.50"))
EDGE_MIN        = float(os.environ.get("EDGE_MIN",  "0.08"))
VIX_BLOCK       = float(os.environ.get("VIX_BLOCK", "30.0"))

# ── DirectionalPredictor thresholds ───────────────────────────────────────
# Primary entry gate replacing MHS >= 75.
#
# DIRECTIONAL_MIN: minimum |DirectionalScore| to consider entry.
#   Score range is -100 to +100. 45 = MEDIUM conviction minimum.
#   Lower values → more trades, more false positives.
#   Higher values → fewer trades, higher precision.
DIRECTIONAL_MIN = float(os.environ.get("DIRECTIONAL_MIN", "45"))

# POLY_PRICE_MAX: maximum YES (or NO) price allowed at entry.
#   Filters out markets where the crowd has already priced the move.
#   0.65 means we only enter when the market assigns ≤65% probability.
#   This ensures there is room for edge — if YES is at 0.90 the crowd
#   already knows what we know.
POLY_PRICE_MAX  = float(os.environ.get("POLY_PRICE_MAX", "0.65"))

# TIME_OFFSET / TIME_OFFSET_WINDOW — dynamic entry window per asset.
#
# Each Polymarket daily market is treated as opening 24h before its resolution time.
# That reference point is when the price is freshest (~0.50, least priced by crowd).
#
#   market_ref   = resolve_dt - 24h   (e.g. crypto noon → yesterday noon)
#   window_start = market_ref + TIME_OFFSET
#   window_end   = market_ref + TIME_OFFSET + TIME_OFFSET_WINDOW
#
# Example with defaults (OFFSET=6, WINDOW=10):
#   Crypto (resolves noon ET):   window = yesterday 18:00 → today 04:00 ET
#   Equity (resolves 4PM ET):   window = yesterday 22:00 → today 08:00 ET
#
# Constraint: TIME_OFFSET + TIME_OFFSET_WINDOW must be < 24.
# If violated at startup, defaults are used and a warning is logged.
#
TIME_OFFSET        = int(os.environ.get("TIME_OFFSET",        "6"))
TIME_OFFSET_WINDOW = int(os.environ.get("TIME_OFFSET_WINDOW", "10"))

# ── Legacy entry window (kept for in_entry_window() equity candle check) ───
ENTRY_HOUR_START: Tuple[int, int] = (4,  30)
ENTRY_HOUR_END:   Tuple[int, int] = (9,  0)

# ── Risk management ────────────────────────────────────────────────────────
STOP_LOSS_PCT   = float(os.environ.get("STOP_LOSS_PCT",   "0.35"))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.50"))
KELLY_FRACTION  = float(os.environ.get("KELLY_FRACTION",  "0.25"))
MAX_POS_PCT     = float(os.environ.get("MAX_POS_PCT",     "0.15"))
# Lowered from 5.0 to 2.0 for paper trading with small capital.
# Kelly on a 50 USDC account with ~52% win rate produces ~1-3 USDC positions.
MIN_POS_USDC    = float(os.environ.get("MIN_POS_USDC",    "2.0"))
KELLY_MIN_TRADES= int(os.environ.get("KELLY_MIN_TRADES",  "10"))
KELLY_FALLBACK  = float(os.environ.get("KELLY_FALLBACK",  "0.05"))
CAPITAL_INITIAL = float(os.environ.get("CAPITAL_INITIAL", "500.0"))

# ── Entry windows (legacy — kept for in_entry_window() equity check) ───────
ENTRY_WINDOW_START: Tuple[int, int] = (9,  30)
ENTRY_WINDOW_END:   Tuple[int, int] = (13, 30)

# ── CLOB execution ─────────────────────────────────────────────────────────
CLOB_MAX_RETRIES = int(os.environ.get("CLOB_MAX_RETRIES", "3"))
CLOB_RETRY_DELAY = float(os.environ.get("CLOB_RETRY_DELAY", "2.0"))
CLOB_LIMIT_SLIP  = float(os.environ.get("CLOB_LIMIT_SLIP",  "0.02"))

# ── API credentials ────────────────────────────────────────────────────────
FRED_KEY     = os.environ.get("FRED_API_KEY",       "")
FINNHUB_KEY  = os.environ.get("FINNHUB_API_KEY",    "")
POLY_PK      = os.environ.get("POLY_PRIVATE_KEY",   "")
POLY_FUNDER  = os.environ.get("POLY_FUNDER_ADDRESS","")
POLY_HOST    = "https://clob.polymarket.com"
POLY_CHAIN   = 137
GAMMA_API    = "https://gamma-api.polymarket.com"

# ── LLM backend ────────────────────────────────────────────────────────────
# Local MLX (Apple Silicon):
#   LLM_BACKEND=openai
#   LLM_BASE_URL=http://192.168.0.90:8181/v1
#   LLM_API_KEY=none
#   LLM_MODEL=mlx-community/Qwen3.5-35B-A3B-4bit
#
LLM_BACKEND  = os.environ.get("LLM_BACKEND", "anthropic").lower()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODEL    = os.environ.get("LLM_MODEL",    "claude-sonnet-4-20250514")
LLM_API_KEY  = os.environ.get("LLM_API_KEY",  os.environ.get("ANTHROPIC_API_KEY", ""))

# ── Confidence cap per model ───────────────────────────────────────────────
MODEL_CONF_CAPS: Dict[str, float] = {
    "claude":      1.00,
    "gpt-4o":      1.00,
    "gpt-4":       1.00,
    "qwen3.5":     0.80,
    "qwen3":       0.75,
    "llama3.3":    0.80,
    "llama-3.3":   0.80,
    "deepseek":    0.75,
    "llama3.1":    0.50,
    "llama3":      0.50,
    "llama-3.1":   0.50,
    "llama3.2":    0.45,
    "llama-3.2":   0.45,
    "mistral":     0.45,
    "phi":         0.35,
    "gemma":       0.40,
}

# ── Database ───────────────────────────────────────────────────────────────
DB_FILE          = os.environ.get("BOT_DB",           "bot.db")
CANDLES_1H_KEEP  = int(os.environ.get("CANDLES_1H_KEEP", "200"))
CANDLES_1D_KEEP  = int(os.environ.get("CANDLES_1D_KEEP", "150"))
HIST_YEARS       = int(os.environ.get("HIST_YEARS",       "4"))

# ── FRED macro series ──────────────────────────────────────────────────────
FRED_SERIES: Dict[str, str] = {
    "VIX":      "VIXCLS",
    "FED_RATE": "FEDFUNDS",
    "T10Y":     "GS10",
    "UNRATE":   "UNRATE",
}

# ── Daily title patterns for Polymarket market discovery ──────────────────
DAILY_TITLE_PATTERNS: Dict[str, list] = {
    "BTC-USD": ["btc", "bitcoin"],
    "ETH-USD": ["eth", "ethereum"],
    "XRP-USD": ["xrp", "ripple"],
    "^GSPC":   ["spx", "s&p", "s&p 500", "sp500"],
    "^NDX":    ["ndx", "nasdaq", "nasdaq 100", "nasdaq-100"],
    "AAPL":    ["apple", "aapl"],
}
DAILY_KEYWORDS = [
    "up today", "go up today", "higher today", "up on",
    "daily up", "price up", "above", "close above", "end above",
]