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
# Each asset maps to its Polymarket daily Up/Down market.
# token_yes / token_no are discovered automatically at startup via Gamma API.
WATCH_ASSETS: Dict[str, dict] = {
    "BTC-USD": {
        "name":             "Bitcoin",
        "poly_slug_prefix": "bitcoin-up-or-down-on",   # slug = prefix-month-day-year
        "poly_slug":        "",                         # built dynamically at runtime
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "crypto",   # crypto | equity_etf
    },
    "ETH-USD": {
        "name":             "Ethereum",
        "poly_slug_prefix": "ethereum-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "crypto",
    },
    "SPX": {
        "name":             "S&P 500",
        "poly_slug_prefix": "spx-up-or-down-on",
        "poly_slug":        "",
        "token_yes":        "",
        "token_no":         "",
        "resolution":       "daily",
        "asset_type":       "equity_etf",
    },
}

# ── Signal thresholds ──────────────────────────────────────────────────────
MHS_MIN_DAILY   = float(os.environ.get("MHS_MIN_DAILY",  "75"))
MHS_MIN_WEEKLY  = float(os.environ.get("MHS_MIN_WEEKLY", "82"))
DBS_LONG_THRESH = float(os.environ.get("DBS_LONG",  "0.50"))
DBS_SHORT_THRESH= float(os.environ.get("DBS_SHORT", "-0.50"))
EDGE_MIN        = float(os.environ.get("EDGE_MIN",  "0.08"))
VIX_BLOCK       = float(os.environ.get("VIX_BLOCK", "30.0"))

# ── Risk management ────────────────────────────────────────────────────────
STOP_LOSS_PCT   = float(os.environ.get("STOP_LOSS_PCT",   "0.35"))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.50"))
KELLY_FRACTION  = float(os.environ.get("KELLY_FRACTION",  "0.25"))
MAX_POS_PCT     = float(os.environ.get("MAX_POS_PCT",     "0.15"))
MIN_POS_USDC    = float(os.environ.get("MIN_POS_USDC",    "5.0"))
KELLY_MIN_TRADES= int(os.environ.get("KELLY_MIN_TRADES",  "10"))
KELLY_FALLBACK  = float(os.environ.get("KELLY_FALLBACK",  "0.05"))
CAPITAL_INITIAL = float(os.environ.get("CAPITAL_INITIAL", "500.0"))

# ── Entry windows (equity only — crypto is always open) ────────────────────
ENTRY_WINDOW_START: Tuple[int, int] = (9,  30)   # 9:30 ET
ENTRY_WINDOW_END:   Tuple[int, int] = (13, 30)   # 13:30 ET

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
POLY_CHAIN   = 137  # Polygon mainnet
GAMMA_API    = "https://gamma-api.polymarket.com"

# ── LLM backend ────────────────────────────────────────────────────────────
# Supports any OpenAI-compatible endpoint (local servers, Groq, Together, OpenAI)
# or the native Anthropic SDK.
#
# Local OpenAI-compatible server:
#   LLM_BACKEND=openai
#   LLM_BASE_URL=http://localhost:11434/v1
#   LLM_MODEL=llama3.2
#   LLM_API_KEY=none         # or whatever your server expects
#
# Claude:
#   LLM_BACKEND=anthropic
#   ANTHROPIC_API_KEY=sk-ant-...
#   LLM_MODEL=claude-sonnet-4-20250514
#
# Groq / Together / OpenAI:
#   LLM_BACKEND=openai
#   LLM_BASE_URL=https://api.groq.com/openai/v1
#   LLM_API_KEY=gsk_...
#   LLM_MODEL=llama-3.3-70b-versatile
#
LLM_BACKEND  = os.environ.get("LLM_BACKEND", "anthropic").lower()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODEL    = os.environ.get("LLM_MODEL",    "claude-sonnet-4-20250514")
LLM_API_KEY  = os.environ.get("LLM_API_KEY",  os.environ.get("ANTHROPIC_API_KEY", ""))

# Confidence cap per model — smaller local models should influence PIP less
MODEL_CONF_CAPS: Dict[str, float] = {
    "claude":   1.00, "gpt-4": 1.00, "gpt-4o": 1.00,
    "llama3.3": 0.80, "llama-3.3": 0.80, "deepseek": 0.75,
    "llama3.2": 0.45, "llama3.1": 0.50, "mistral": 0.45,
    "llama3":   0.50, "phi": 0.35, "gemma": 0.40,
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
    "SPX":     ["spx", "s&p", "s&p 500", "sp500"],
}
DAILY_KEYWORDS = [
    "up today", "go up today", "higher today", "up on",
    "daily up", "price up", "above", "close above", "end above",
]