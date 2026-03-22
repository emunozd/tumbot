# tumbot

A systematic trading bot for [Polymarket](https://polymarket.com) daily Up/Down prediction markets. It combines real-time technical analysis, macro indicators, and LLM-powered sentiment to identify mispricings — cases where the market's implied probability diverges from the bot's own estimate by at least 8%.

---

## How it works

The bot calculates two scores for each watched asset every minute:

**Market Heat Score (MHS, 0–100)** — *should we trade at all right now?*

| Component | Weight | Source |
|---|---|---|
| Technical | 40% | RSI, MACD, MA cross, Bollinger %B, volume ratio |
| Sentiment | 35% | LLM NLP on news headlines + Fear & Greed estimate |
| Macro | 25% | VIX, yield curve spread (T10Y − Fed Funds), unemployment |

**Directional Bias Score (DBS, −1 to +1)** — *which way is the asset going?*

| Voter | Weight |
|---|---|
| MA cross (MA5/MA20/MA50, golden/death cross) | 30% |
| Momentum (RSI vs 50, MACD histogram) | 25% |
| Volume directional confirmation | 20% |
| Macro + NLP direction bias | 25% |

The DBS is converted into our own **implied probability (PIP)** via `PIP = 0.50 + (DBS × 0.40)`, then validated through a Bayesian update with the LLM. If the resulting PIP exceeds the market's current YES price by at least 8%, that is the edge we trade on.

### Entry logic

```
MHS ≥ 75  AND  DBS ≥ +0.50  AND  pip > yes_price + 0.08  →  BUY YES
MHS ≥ 75  AND  DBS ≤ −0.50  AND  (1-pip) > no_price + 0.08  →  BUY NO
VIX > 30  →  BLOCKED (no new entries)
```

### Exit logic (checked every minute)

1. CLOB price ≥ 0.97 → market resolved in our favour → **WON**
2. CLOB price ≤ 0.03 → market resolved against us → **LOST**
3. CLOB price ≤ stop-loss price → **STOP_LOSS** (max 35% of invested USDC)
4. CLOB price ≥ take-profit price → **TAKE_PROFIT** (50% gain on invested USDC)
5. MHS < 50 or direction flips → **SIGNAL_REVERSED** (sell back to market)

### Position sizing

Kelly Criterion with quarter-Kelly conservative factor:

```
b  = (1 - market_price) / market_price   # net payout ratio
f* = (pip × b − (1−pip)) / b             # full Kelly fraction
bet = capital × f* × 0.25               # quarter-Kelly
```

Once 10+ trades are recorded, the win rate from actual history blends into the PIP estimate, adapting the sizing to real performance.

---

## Architecture (SOLID)

```
tumbot/
├── main.py                    # thin entry point — clock loop only
└── src/
    ├── config.py              # all constants, all env vars (Single Responsibility)
    ├── models.py              # pure data classes, zero logic
    ├── data/
    │   ├── database.py        # SQLite persistence (all DB I/O lives here)
    │   └── market_data.py     # yfinance, FRED, Polymarket price fetching
    ├── signals/
    │   ├── indicators.py      # pure math functions (RSI, MACD, EMA, …)
    │   └── scoring.py         # MHS, DBS, PIP computation
    ├── llm/
    │   ├── client.py          # provider-agnostic LLM wrapper
    │   └── analysis.py        # sentiment analysis + Bayesian PIP validation
    ├── trading/
    │   ├── sizing.py          # Kelly sizing, stop/TP calculation
    │   ├── execution.py       # CLOB order placement with retry
    │   └── engine.py          # signal evaluation + position lifecycle
    └── ui/
        └── display.py         # Rich terminal rendering
```

Each module has a single responsibility and depends only on modules below it in the hierarchy. `main.py` knows about everything; `indicators.py` knows about nothing.

---

## Database

SQLite (`bot.db`) — no server, no setup, one file.

| Table | Purpose | Grows? |
|---|---|---|
| `assets` | ticker catalogue and Polymarket token IDs | fixed |
| `candles_1h` | rolling 200-bar hourly window per ticker | capped |
| `candles_1d` | rolling 150-bar daily window per ticker | capped |
| `candles_hist` | full multi-year daily history | unbounded |
| `trades` | all closed trades | unbounded |
| `positions` | currently open positions | small |
| `portfolio` | single-row capital snapshot | 1 row |
| `daily_snapshots` | daily metrics for charting | 1 row/day |

**Candle update cadence:**
- `candles_1h` — updated at HH:01 (one new candle per hour)
- `candles_1d` — updated at 00:01 ET (one new candle per day)
- `candles_hist` — delta-only updates: if the bot was offline for 5 days, it fetches exactly those 5 days on restart, not the full history again

---

## LLM backends

The bot works with any of these — set `LLM_BACKEND` accordingly:

| Backend | Cost | How to use |
|---|---|---|
| Anthropic (Claude) | paid | `LLM_BACKEND=anthropic` + `ANTHROPIC_API_KEY` |
| Local model (any OpenAI-compatible server) | free | `LLM_BACKEND=openai` + `LLM_BASE_URL=http://your-server:port/v1` |
| Groq | free tier | `LLM_BACKEND=openai` + Groq API key |
| Together AI | free tier | `LLM_BACKEND=openai` + Together API key |
| OpenAI | paid | `LLM_BACKEND=openai` + `OPENAI_API_KEY` |

The bot automatically caps the LLM's influence on PIP based on model capability. A small local model (Llama 3.2 7B) gets a 45% weight cap; Claude or GPT-4 get 100%.

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/your-username/tumbot.git
cd tumbot
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your keys
```

Minimum required:
```bash
FRED_API_KEY=your_key      # free at fred.stlouisfed.org
LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Run in paper mode (no real money)

```bash
python main.py
```

The bot will:
1. Create `bot.db` and load 4 years of price history per ticker (~10s first run)
2. Discover today's active Polymarket markets automatically
3. Start displaying the live dashboard

### 4. Run in live mode (real USDC)

Requires a Polygon wallet with USDC deposited on Polymarket:

```bash
export POLY_PRIVATE_KEY=0x...
export POLY_FUNDER_ADDRESS=0x...
python main.py
```

> ⚠️ **Risk warning**: Polymarket positions are binary. If the market resolves against you, you lose 100% of the USDC invested in that position. The stop-loss mechanism can limit this to ~35% by selling before resolution, but it is not guaranteed if the CLOB lacks liquidity. Never invest more than you can afford to lose.

---

## Entry window

| Asset type | Entry window |
|---|---|
| Crypto (BTC, ETH) | 24 hours / 7 days — always open |
| Equity ETF (QQQ) | Mon–Fri, 9:30–13:30 ET only |

Crypto prediction markets on Polymarket are active around the clock, so restricting them to NYSE hours would discard valid signals during nights and weekends.

---

## Run tests

```bash
pytest tests/ -v
```

Tests cover all pure-function modules (indicators, scoring, sizing) with no network calls or DB dependency.

---

## Adding a new asset

1. Add the ticker to `WATCH_ASSETS` in `src/config.py`:

```python
"SOL-USD": {
    "name":       "Solana",
    "poly_slug":  "will-sol-price-go-up-today",
    "token_yes":  "",
    "token_no":   "",
    "resolution": "daily",
    "asset_type": "crypto",
},
```

2. The bot discovers the Polymarket token IDs automatically at startup.
3. Price history is downloaded from yfinance on the first run.

---

## Environment variables reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `FRED_API_KEY` | Yes | — | FRED API key |
| `LLM_BACKEND` | No | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` | If using Claude | — | Anthropic API key |
| `LLM_BASE_URL` | If using local server/Groq | — | OpenAI-compatible endpoint |
| `LLM_API_KEY` | If using OpenAI-compat | — | API key for that provider |
| `LLM_MODEL` | No | `claude-sonnet-4-20250514` | Model name |
| `FINNHUB_API_KEY` | No | — | For real-time news headlines |
| `POLY_PRIVATE_KEY` | Live mode only | — | Polygon wallet private key |
| `POLY_FUNDER_ADDRESS` | Live mode only | — | Wallet address holding USDC |
| `CAPITAL_INITIAL` | No | `500.0` | Starting paper capital in USDC |
| `MHS_MIN_DAILY` | No | `75` | Minimum MHS to enter daily markets |
| `EDGE_MIN` | No | `0.08` | Minimum edge over market price |
| `STOP_LOSS_PCT` | No | `0.35` | Stop-loss as fraction of entry price |
| `TAKE_PROFIT_PCT` | No | `0.50` | Take-profit as fraction of entry price |
| `KELLY_FRACTION` | No | `0.25` | Kelly multiplier (quarter-Kelly) |
| `BOT_DB` | No | `bot.db` | SQLite file path |

---

## Running with Docker

### Prerequisites
- Docker 24+ and Docker Compose v2+
- Your `.env` file configured (see Setup above)

### Quick start

```bash
# 1. Create the data directory on the host
# Docker mounts this path into the container for the SQLite database.
# Must exist with write permissions before running docker compose.
sudo mkdir -p /var/lib/tumbot/data
sudo chmod 777 /var/lib/tumbot/data

# 2. Clone and configure
git clone https://github.com/your-username/tumbot.git
cd tumbot
cp .env.example .env
# Fill in FRED_API_KEY and LLM credentials in .env

# 3. Build and run
docker compose up --build

# 4. Run in background
docker compose up --build -d

# 5. View live logs
docker logs -f tumbot
```

> **Note:** The `/var/lib/tumbot/data` directory on the host persists the SQLite
> database across container restarts and rebuilds. Only `docker compose down -v`
> removes it.

### Useful commands

```bash
# Stop the bot
docker compose down

# Stop and wipe the database (start fresh)
docker compose down -v

# Rebuild after code changes
docker compose up --build

# Open a shell inside the running container
docker exec -it tumbot bash

# Run tests inside the container
docker exec tumbot python -m pytest tests/ -v
```

### Data persistence

The SQLite database is stored in a Docker named volume (`bot_data`) mounted at `/data/bot.db` inside the container. It survives container restarts and rebuilds. Only `docker compose down -v` removes it.

To back up the database:

```bash
docker cp tumbot:/data/bot.db ./bot_backup_$(date +%Y%m%d).db
```


---

## Disclaimer

This software is provided for educational purposes. It is not financial advice. Prediction market trading involves significant risk of loss. Past performance of any backtested signals does not guarantee future results.