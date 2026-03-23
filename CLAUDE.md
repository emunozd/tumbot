# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Tumbot is a systematic trading bot for Polymarket daily Up/Down prediction markets. It fuses technical analysis, macro indicators, and LLM sentiment validation to detect mispricings, then executes trades via the Polymarket CLOB API.

## Commands

```bash
# Run the bot (paper trading by default)
python main.py

# Run tests
pytest tests/ -v

# Run a single test file
pytest tests/test_indicators.py -v

# Backtest
python backtest_btc.py --days 365 --edge 0.06

# Docker
docker compose up --build
docker compose down
docker compose down -v   # also wipes the SQLite database
```

## Architecture

The app follows strict SOLID principles with unidirectional dependencies:

```
main.py → trading/engine.py → signals/scoring.py → signals/indicators.py
                            → llm/analysis.py
                            → data/market_data.py → data/database.py
                            → trading/sizing.py
                            → trading/execution.py
        → ui/display.py
        → telegram/bot.py
```

**Key invariants:**
- `src/models.py` — pure dataclasses only, no logic, no imports from src
- `src/signals/indicators.py` — pure math functions, no I/O, no state
- `src/config.py` — single source of truth for all constants and thresholds; never hardcode values elsewhere

## Core Signal Pipeline

Every 60 seconds, `main.py` runs this flow per asset:

1. **MHS** (Market Heat Score, 0–100): 40% technical + 35% sentiment + 25% macro
2. **DBS** (Directional Bias Score, -1 to +1): MA cross 30%, momentum 25%, volume 20%, macro 25%
3. **PIP** (Probability Estimate): `0.50 + DBS × 0.40`
4. **LLM validation** (optional): Bayesian adjustment of PIP if `MHS ≥ 70`
5. **Edge**: `|PIP - market_price|`; trade if `edge ≥ EDGE_MIN` (default 0.08) and `MHS ≥ MHS_MIN_DAILY` (default 75)

Exit conditions checked every cycle: CLOB price ≥ 0.97 (WON), ≤ 0.03 (LOST), stop-loss, take-profit, or signal reversal.

## LLM Integration

`src/llm/client.py` wraps both Anthropic and OpenAI SDKs behind a single `llm_chat()` function. LLM influence on PIP is capped by model capability (defined in `config.py`): Claude = 1.0, smaller models = 0.45. If no LLM is configured, the bot runs without it.

## Data Storage

SQLite at `bot.db` (path via `BOT_DB` env var, Docker path `/data/bot.db`). Candle tables are auto-pruned (200 hourly / 150 daily rows per ticker). The `portfolio` table always has exactly one row — update it, never insert.

## Environment

Copy `.env.example` to `.env`. Only `FRED_API_KEY` is strictly required for paper trading. `POLY_PRIVATE_KEY` + `POLY_FUNDER_ADDRESS` unlock live order execution. `LLM_BACKEND` switches between `anthropic` and `openai`; set `LLM_BASE_URL` for Groq/Together/local endpoints.

## Testing

Tests cover pure functions only (`indicators`, `scoring`, `sizing`). Integration with external APIs (yfinance, FRED, Polymarket, LLM) is not mocked — avoid adding mocks. `backtest_btc.py` is standalone and does not depend on the installed bot state.
