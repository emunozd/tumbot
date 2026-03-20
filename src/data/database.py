"""
data/database.py — SQLite persistence layer.

Single Responsibility: all database I/O lives here, nothing else.
The rest of the bot never touches SQLite directly — it calls these functions.

Schema:
  assets        — catalogue of watched tickers and their Polymarket token IDs
  candles_1h    — rolling window of hourly OHLCV (capped at CANDLES_1H_KEEP rows/ticker)
  candles_1d    — rolling window of daily OHLCV  (capped at CANDLES_1D_KEEP rows/ticker)
  candles_hist  — full multi-year daily history (unlimited, insert-only)
  trades        — all closed trades (unlimited, insert-only, queried with LIMIT)
  positions     — currently open positions (small table, always current)
  portfolio     — single-row capital snapshot
  daily_snapshots — one row per day with portfolio metrics for charting
"""

import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import asdict

from src.config import DB_FILE, CANDLES_1H_KEEP, CANDLES_1D_KEEP, ET


# ── Connection ─────────────────────────────────────────────────────────────

@contextmanager
def _conn():
    """
    Thread-safe SQLite connection via context manager.
    WAL mode allows concurrent reads while a write is in progress.
    Each call opens and closes its own connection — no shared state.
    """
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    with _conn() as conn:
        conn.executescript("""

        CREATE TABLE IF NOT EXISTS assets (
            ticker          TEXT PRIMARY KEY,
            name            TEXT,
            asset_type      TEXT,
            poly_slug       TEXT DEFAULT '',
            token_yes       TEXT DEFAULT '',
            token_no        TEXT DEFAULT '',
            condition_id    TEXT DEFAULT '',
            market_question TEXT DEFAULT '',
            hist_loaded     INTEGER DEFAULT 0,
            last_1h_ts      INTEGER DEFAULT 0,
            last_1d_ts      INTEGER DEFAULT 0,
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS candles_1h (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker  TEXT    NOT NULL,
            ts      INTEGER NOT NULL,
            open    REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(ticker, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_1h ON candles_1h(ticker, ts DESC);

        CREATE TABLE IF NOT EXISTS candles_1d (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker  TEXT    NOT NULL,
            ts      INTEGER NOT NULL,
            open    REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(ticker, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_1d ON candles_1d(ticker, ts DESC);

        CREATE TABLE IF NOT EXISTS candles_hist (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker  TEXT    NOT NULL,
            ts      INTEGER NOT NULL,
            open    REAL, high REAL, low REAL, close REAL, volume REAL,
            UNIQUE(ticker, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_hist ON candles_hist(ticker, ts DESC);

        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            asset        TEXT, side TEXT,
            entry_price  REAL, exit_price REAL, shares REAL,
            pnl          REAL, pnl_pct REAL, log_return REAL,
            ev_at_entry  REAL, pip_at_entry REAL,
            duration     TEXT, reason TEXT, time TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time DESC);

        CREATE TABLE IF NOT EXISTS positions (
            asset           TEXT PRIMARY KEY,
            side            TEXT, token_id TEXT,
            shares          REAL, entry_price REAL, usdc_spent REAL,
            stop_loss       REAL, take_profit REAL,
            entry_time      TEXT, resolution TEXT,
            entry_mhs       REAL, entry_dbs REAL, entry_pip REAL,
            order_id        TEXT DEFAULT '',
            status          TEXT DEFAULT 'OPEN'
        );

        CREATE TABLE IF NOT EXISTS portfolio (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            capital_usdc  REAL,
            peak_capital  REAL,
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            date          TEXT PRIMARY KEY,
            capital_usdc  REAL, total_pnl REAL,
            log_return    REAL, trades_count INTEGER, win_rate REAL
        );

        """)


# ── Assets ─────────────────────────────────────────────────────────────────

def upsert_asset(ticker: str, name: str, asset_type: str, poly_slug: str = "") -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO assets (ticker, name, asset_type, poly_slug, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name=excluded.name, asset_type=excluded.asset_type,
                poly_slug=excluded.poly_slug, updated_at=excluded.updated_at
        """, (ticker, name, asset_type, poly_slug, datetime.now(ET).isoformat()))


def update_asset_tokens(ticker: str, token_yes: str, token_no: str,
                        question: str = "", condition_id: str = "") -> None:
    with _conn() as conn:
        conn.execute("""
            UPDATE assets
            SET token_yes=?, token_no=?, market_question=?,
                condition_id=?, updated_at=?
            WHERE ticker=?
        """, (token_yes, token_no, question, condition_id,
               datetime.now(ET).isoformat(), ticker))


def set_hist_loaded(ticker: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE assets SET hist_loaded=1, updated_at=? WHERE ticker=?",
            (datetime.now(ET).isoformat(), ticker)
        )


def get_asset(ticker: str) -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE ticker=?", (ticker,)).fetchone()
        return dict(row) if row else {}


# ── Candle helpers ─────────────────────────────────────────────────────────

def _insert_candles(table: str, ticker: str, candles: List[dict]) -> None:
    if not candles:
        return
    with _conn() as conn:
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} "
            f"(ticker,ts,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            [(ticker, c["t"], c["o"], c["h"], c["l"], c["c"], c.get("v", 0))
             for c in candles]
        )


def _trim_rolling(table: str, ticker: str, keep: int) -> None:
    """Delete oldest rows keeping only the `keep` most recent by timestamp."""
    with _conn() as conn:
        conn.execute(f"""
            DELETE FROM {table}
            WHERE ticker=? AND ts IN (
                SELECT ts FROM {table} WHERE ticker=?
                ORDER BY ts ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM {table} WHERE ticker=?) - ?)
            )
        """, (ticker, ticker, ticker, keep))


def _last_ts(table: str, ticker: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            f"SELECT MAX(ts) FROM {table} WHERE ticker=?", (ticker,)
        ).fetchone()[0]
        return row or 0


# ── Candle writes ──────────────────────────────────────────────────────────

def save_candles_1h(ticker: str, candles: List[dict]) -> None:
    _insert_candles("candles_1h", ticker, candles)
    _trim_rolling("candles_1h", ticker, CANDLES_1H_KEEP)
    if candles:
        with _conn() as conn:
            conn.execute(
                "UPDATE assets SET last_1h_ts=?, updated_at=? WHERE ticker=?",
                (max(c["t"] for c in candles), datetime.now(ET).isoformat(), ticker)
            )


def save_candles_1d(ticker: str, candles: List[dict]) -> None:
    _insert_candles("candles_1d", ticker, candles)
    _trim_rolling("candles_1d", ticker, CANDLES_1D_KEEP)
    if candles:
        with _conn() as conn:
            conn.execute(
                "UPDATE assets SET last_1d_ts=?, updated_at=? WHERE ticker=?",
                (max(c["t"] for c in candles), datetime.now(ET).isoformat(), ticker)
            )


def save_candles_hist(ticker: str, candles: List[dict]) -> None:
    """Historical candles — insert-only, no trim."""
    _insert_candles("candles_hist", ticker, candles)


# ── Candle reads ───────────────────────────────────────────────────────────

def _rows_to_candles(rows) -> List[dict]:
    return [{"t": r["ts"], "o": r["open"], "h": r["high"],
             "l": r["low"],  "c": r["close"], "v": r["volume"]}
            for r in reversed(rows)]


def load_candles_1h(ticker: str, limit: int = 100) -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM candles_1h WHERE ticker=? ORDER BY ts DESC LIMIT ?",
            (ticker, limit)
        ).fetchall()
    return _rows_to_candles(rows)


def load_candles_1d(ticker: str, limit: int = 100) -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM candles_1d WHERE ticker=? ORDER BY ts DESC LIMIT ?",
            (ticker, limit)
        ).fetchall()
    return _rows_to_candles(rows)


def get_last_1h_ts(ticker: str) -> int:
    return _last_ts("candles_1h", ticker)


def get_last_1d_ts(ticker: str) -> int:
    return _last_ts("candles_1d", ticker)


def get_last_hist_ts(ticker: str) -> int:
    return _last_ts("candles_hist", ticker)


# ── Trades ─────────────────────────────────────────────────────────────────

def save_trade(trade) -> int:
    d = trade if isinstance(trade, dict) else asdict(trade)
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
                (asset,side,entry_price,exit_price,shares,pnl,pnl_pct,
                 log_return,ev_at_entry,pip_at_entry,duration,reason,time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (d["asset"], d["side"], d["entry_price"], d["exit_price"],
              d["shares"], d["pnl"], d["pnl_pct"], d.get("log_return", 0),
              d.get("ev_at_entry", 0), d.get("pip_at_entry", 0),
              d["duration"], d["reason"], d["time"]))
        return cur.lastrowid


def load_recent_trades(limit: int = 200) -> List[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def trade_stats() -> dict:
    """Aggregate stats computed in SQL — efficient regardless of table size."""
    with _conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS winners,
                SUM(pnl)        AS total_pnl,
                SUM(log_return) AS total_log_return,
                AVG(ev_at_entry)AS avg_ev
            FROM trades
        """).fetchone()
    return dict(row) if row else {}


# ── Positions ──────────────────────────────────────────────────────────────

def save_position(asset: str, pos) -> None:
    d = pos if isinstance(pos, dict) else asdict(pos)
    entry_time = d["entry_time"]
    if not isinstance(entry_time, str):
        entry_time = entry_time.isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO positions
                (asset,side,token_id,shares,entry_price,usdc_spent,
                 stop_loss,take_profit,entry_time,resolution,
                 entry_mhs,entry_dbs,entry_pip,order_id,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (asset, d["side"], d.get("token_id",""),
              d["shares"], d["entry_price"], d["usdc_spent"],
              d.get("stop_loss",0), d.get("take_profit",1),
              entry_time, d.get("resolution","daily"),
              d.get("entry_mhs",0), d.get("entry_dbs",0), d.get("entry_pip",0.5),
              d.get("order_id",""), d.get("status","OPEN")))


def delete_position(asset: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM positions WHERE asset=?", (asset,))


def load_open_positions() -> Dict[str, dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='OPEN'"
        ).fetchall()
    return {r["asset"]: dict(r) for r in rows}


# ── Portfolio ──────────────────────────────────────────────────────────────

def save_portfolio(capital: float, peak: float) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO portfolio (id, capital_usdc, peak_capital, updated_at)
            VALUES (1, ?, ?, ?)
        """, (capital, peak, datetime.now(ET).isoformat()))


def load_portfolio() -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM portfolio WHERE id=1").fetchone()
        return dict(row) if row else {}


def save_daily_snapshot(capital: float, trades_today: List[dict]) -> None:
    today = datetime.now(ET).strftime("%Y-%m-%d")
    pnl   = sum(t["pnl"] for t in trades_today) if trades_today else 0
    lr    = sum(t.get("log_return", 0) for t in trades_today)
    wins  = sum(1 for t in trades_today if t["pnl"] > 0)
    wr    = wins / len(trades_today) if trades_today else 0
    with _conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_snapshots
                (date, capital_usdc, total_pnl, log_return, trades_count, win_rate)
            VALUES (?,?,?,?,?,?)
        """, (today, capital, pnl, lr, len(trades_today), wr))


# ── Diagnostics ────────────────────────────────────────────────────────────

def db_stats() -> dict:
    with _conn() as conn:
        r = {}
        for t in ["trades","candles_1h","candles_1d","candles_hist"]:
            r[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    r["size_kb"] = os.path.getsize(DB_FILE) / 1024 if os.path.exists(DB_FILE) else 0
    return r
