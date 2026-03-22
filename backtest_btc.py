#!/usr/bin/env python3
"""
backtest_btc.py — Prueba de escritorio para tumbot usando datos históricos de BTC.

Simula la lógica de señales de tumbot (MHS, DBS, PIP, Edge) sobre velas diarias
históricas de BTC-USD y muestra en qué días hubiera entrado y en qué dirección.

Uso:
    python backtest_btc.py                          # últimos 180 días
    python backtest_btc.py --days 365               # último año
    python backtest_btc.py --start 2024-01-01       # desde fecha específica
    python backtest_btc.py --days 90 --edge 0.06    # edge mínimo más bajo

NO requiere tumbot instalado — todo es independiente (solo yfinance + pandas).
"""

import argparse
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional, List

# ── Dependencias ───────────────────────────────────────────────────────────
try:
    import yfinance as yf
except ImportError:
    print("⚠  yfinance no instalado: pip install yfinance")
    raise

try:
    import pandas as pd
except ImportError:
    print("⚠  pandas no instalado: pip install pandas")
    raise

# ── Indicadores técnicos (espejo de src/signals/indicators.py) ─────────────

def sma(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return round(statistics.mean(prices[-period:]), 6)

def ema(prices: list, period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    e = prices[0]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
    return round(e, 6)

def rsi(prices: list, period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = statistics.mean(gains[-period:])
    al = statistics.mean(losses[-period:])
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - (100 / (1 + rs)), 2)

def macd(prices: list, fast=12, slow=26, signal_p=9) -> dict:
    if len(prices) < slow + signal_p:
        return {"macd": None, "signal": None, "hist": None}
    e_fast   = ema(prices, fast)
    e_slow   = ema(prices, slow)
    macd_val = round(e_fast - e_slow, 6) if e_fast and e_slow else None
    # approximate signal with SMA of last 9 MACD values
    hist_prices = []
    for i in range(max(0, len(prices) - signal_p - 5), len(prices)):
        ef = ema(prices[:i+1], fast)
        es = ema(prices[:i+1], slow)
        if ef and es:
            hist_prices.append(ef - es)
    sig = sma(hist_prices, signal_p) if len(hist_prices) >= signal_p else None
    hist = round(macd_val - sig, 6) if macd_val and sig else None
    return {"macd": macd_val, "signal": sig, "hist": hist}

def bollinger(prices: list, period: int = 20, k: float = 2.0) -> dict:
    if len(prices) < period:
        return {"upper": None, "lower": None, "mid": None, "pct_b": None}
    sub  = prices[-period:]
    mid  = statistics.mean(sub)
    std  = statistics.pstdev(sub)
    upper = mid + k * std
    lower = mid - k * std
    pct_b = (prices[-1] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    return {"upper": round(upper,4), "lower": round(lower,4),
            "mid": round(mid,4), "pct_b": round(pct_b,4)}

def ma_cross(prices: list) -> str:
    if len(prices) < 22:
        return "none"
    m5n,  m20n  = sma(prices,      5), sma(prices,      20)
    m5p,  m20p  = sma(prices[:-1], 5), sma(prices[:-1], 20)
    if not all([m5n, m20n, m5p, m20p]):
        return "none"
    if m5p <= m20p and m5n > m20n: return "golden"
    if m5p >= m20p and m5n < m20n: return "death"
    return "none"

def volume_ratio(candles: list, period: int = 20) -> Optional[float]:
    vols = [c["v"] for c in candles if c.get("v", 0) > 0]
    if len(vols) < period + 1:
        return None
    avg = statistics.mean(vols[-period-1:-1])
    return round(vols[-1] / avg, 2) if avg > 0 else None

def technicals(candles: list) -> dict:
    prices = [c["c"] for c in candles]
    return {
        "cur":       prices[-1] if prices else None,
        "rsi":       rsi(prices),
        "ma5":       sma(prices, 5),
        "ma20":      sma(prices, 20),
        "ma50":      sma(prices, 50),
        "ema9":      ema(prices, 9),
        "macd":      macd(prices),
        "bb":        bollinger(prices),
        "vol_ratio": volume_ratio(candles),
        "cross":     ma_cross(prices),
    }

# ── Scoring (espejo de src/signals/scoring.py) ────────────────────────────

def compute_mhs_daily(candles: list) -> dict:
    """MHS simplificado para backtesting (sin sentimiento/macro reales)."""
    t   = technicals(candles)
    rsi_v  = t["rsi"]
    cur    = t["cur"]
    ma5, ma20, ma50 = t["ma5"], t["ma20"], t["ma50"]
    mh  = t["macd"]["hist"]
    pb  = t["bb"]["pct_b"]
    vr  = t["vol_ratio"]

    s = 50.0

    if rsi_v:
        if   rsi_v < 25: s += 20
        elif rsi_v < 35: s += 12
        elif rsi_v < 45: s += 5
        elif rsi_v > 75: s -= 20
        elif rsi_v > 65: s -= 12
        elif rsi_v > 55: s -= 5

    if cur and ma20: s += 10 if cur > ma20 else -10
    if ma5 and ma20: s += 8  if ma5 > ma20  else -8
    if cur and ma50: s += 5  if cur > ma50  else -5

    if mh is not None:
        if   mh > 0.05: s += 10
        elif mh > 0:    s += 5
        elif mh < -0.05: s -= 10
        elif mh < 0:    s -= 5

    if pb is not None:
        if   pb < 0.10: s += 8
        elif pb < 0.30: s += 3
        elif pb > 0.90: s -= 8
        elif pb > 0.70: s -= 3

    if vr:
        if   vr > 1.8: s += 7
        elif vr > 1.3: s += 3
        elif vr < 0.5: s -= 4

    cross = t["cross"]
    if cross == "golden": s += 8
    if cross == "death":  s -= 8

    score = max(0.0, min(100.0, s))
    if   score >= 81: zone = "BULL_STRONG"
    elif score >= 66: zone = "BULL"
    elif score >= 46: zone = "NEUTRAL"
    elif score >= 31: zone = "BEAR"
    else:             zone = "BEAR_STRONG"

    return {"score": round(score, 1), "zone": zone}


def compute_dbs_daily(candles: list) -> dict:
    """DBS simplificado para backtesting (sin sentimiento/macro reales)."""
    t   = technicals(candles)
    cur, ma5, ma20, ma50 = t["cur"], t["ma5"], t["ma20"], t["ma50"]
    rsi_v, cross, macd_d = t["rsi"], t["cross"], t["macd"]
    vr = t["vol_ratio"]

    votes = []

    # 1. MA cross (30%)
    ms = 0.0
    if ma5 and ma20 and cur:
        ms += 0.6 if (ma5 > ma20 and cur > ma20) else -0.6
        ms += 0.2 if cur > ma20 else -0.2
        if ma50: ms += 0.2 if cur > ma50 else -0.2
    if cross == "golden": ms = min(1.0, ms + 0.3)
    if cross == "death":  ms = max(-1.0, ms - 0.3)
    votes.append((max(-1, min(1, ms)), 0.30))

    # 2. Momentum (25%)
    mom = 0.0
    if rsi_v: mom += (rsi_v - 50) / 50
    if macd_d["hist"] is not None:
        h = macd_d["hist"]
        mom += max(-0.5, min(0.5, (1 if h > 0 else -1) * 0.4))
    if macd_d["macd"] and macd_d["signal"]:
        mom += 0.2 if macd_d["macd"] > macd_d["signal"] else -0.2
    votes.append((max(-1, min(1, mom)), 0.25))

    # 3. Volumen (20%) — sin precio Polymarket usamos solo vol_ratio neutro
    vs = 0.0
    if vr:
        prev_close = candles[-2]["c"] if len(candles) >= 2 else candles[-1]["c"]
        day_chg    = candles[-1]["c"] - prev_close
        vs = (1 if day_chg >= 0 else -1) * min(1.0, vr / 2.0)
    votes.append((vs, 0.20))

    # 4. Macro neutral (sin FRED en backtest) — peso 25%
    votes.append((0.0, 0.25))

    dbs = max(-1.0, min(1.0, round(sum(s * w for s, w in votes), 3)))
    long_v  = sum(1 for s, _ in votes if s > 0.1)
    short_v = sum(1 for s, _ in votes if s < -0.1)
    agreement = max(long_v, short_v)

    DBS_LONG  =  0.50
    DBS_SHORT = -0.50
    if   dbs >= DBS_LONG  and agreement >= 3: direction = "LONG"
    elif dbs <= DBS_SHORT and agreement >= 3: direction = "SHORT"
    else:                                     direction = "NEUTRAL"

    return {
        "score": dbs, "direction": direction, "agreement": agreement,
        "votes": {
            "ma_cross": round(votes[0][0], 3),
            "momentum": round(votes[1][0], 3),
            "volume":   round(votes[2][0], 3),
            "macro":    0.0,
        },
    }

def compute_pip(dbs_score: float) -> float:
    return round(max(0.30, min(0.70, 0.50 + dbs_score * 0.40)), 3)

def daily_trend(candles: list) -> str:
    prices = [c["c"] for c in candles]
    ma5  = sma(prices, 5)
    ma20 = sma(prices, 20)
    cur  = prices[-1] if prices else None
    if not all([ma5, ma20, cur]):
        return "neutral"
    if ma5 > ma20 and cur > ma20: return "up"
    if ma5 < ma20 and cur < ma20: return "down"
    return "neutral"

# ── Kelly sizing ─────────────────────────────────────────────────────────────

def kelly_size(pip: float, market_price: float, capital: float,
               win_rate: Optional[float] = None) -> float:
    """Quarter-Kelly, 15% cap."""
    p = win_rate if win_rate else pip
    q = 1 - p
    b = (1 - market_price) / market_price  # odds
    kelly_full = (p * b - q) / b if b > 0 else 0.05
    fraction   = max(0, kelly_full) * 0.25  # quarter Kelly
    fraction   = min(fraction, 0.15)         # 15% cap
    if fraction < 0.01:
        fraction = 0.05                      # fallback 5%
    return round(capital * fraction, 2)

# ── Backtester ───────────────────────────────────────────────────────────────

WARMUP_CANDLES = 55  # necesitamos al menos esto para MA50 + MACD

def run_backtest(
    ticker: str = "BTC-USD",
    days:   int = 180,
    start:  Optional[str] = None,
    mhs_min: float = 60.0,     # umbral MHS (liviano para backtest sin macro/sentiment)
    edge_min: float = 0.08,
    capital: float = 500.0,
    assumed_market_price: float = 0.50,  # precio Polymarket asumido (mercado justo)
):
    print(f"\n{'='*72}")
    print(f"  tumbot — Backtest de escritorio")
    print(f"  Activo: {ticker}  |  Capital inicial: ${capital:.2f} USDC")
    print(f"  MHS mínimo: {mhs_min}  |  Edge mínimo: {edge_min}")
    print(f"  Precio Polymarket asumido: {assumed_market_price:.2f} (mercado justo)")
    print(f"{'='*72}")

    # ── Descargar datos ──────────────────────────────────────────────────────
    if start:
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt   = datetime.now()
        extra_start = start_dt - timedelta(days=WARMUP_CANDLES + 5)
    else:
        end_dt   = datetime.now()
        extra_start = end_dt - timedelta(days=days + WARMUP_CANDLES + 5)

    print(f"\n⏳ Descargando {ticker} de yfinance...")
    df = yf.download(ticker, start=extra_start.strftime("%Y-%m-%d"),
                     end=end_dt.strftime("%Y-%m-%d"), interval="1d",
                     progress=False, auto_adjust=True)

    if df.empty:
        print(f"❌ No se obtuvieron datos para {ticker}")
        return

    # Flatten MultiIndex si existe
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]

    print(f"✓ {len(df)} velas descargadas ({df['date'].iloc[0].strftime('%Y-%m-%d')} → {df['date'].iloc[-1].strftime('%Y-%m-%d')})")

    # Convertir a lista de dicts (formato tumbot)
    all_candles = []
    for _, row in df.iterrows():
        ts = int(row["date"].timestamp()) if hasattr(row["date"], "timestamp") else 0
        all_candles.append({
            "t": ts,
            "date": str(row["date"])[:10],
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
            "v": float(row.get("volume", 0)),
        })

    # ── Determinar ventana de análisis ───────────────────────────────────────
    if start:
        analysis_start = start_dt.strftime("%Y-%m-%d")
    else:
        analysis_start = (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")

    # ── Iterar día por día ────────────────────────────────────────────────────
    entries   = []
    sim_cap   = capital
    open_pos  = None
    wins      = 0
    losses    = 0
    total_pnl = 0.0

    print(f"\n📅 Analizando desde {analysis_start}...\n")
    print(f"{'Fecha':<12} {'Precio':>9} {'MHS':>6} {'DBS':>7} {'PIP':>6} {'Dir':<8} "
          f"{'Edge':>7} {'Señal':<20} {'Acción':<25}")
    print("-" * 110)

    for i in range(WARMUP_CANDLES, len(all_candles)):
        candle_today = all_candles[i]
        date_str     = candle_today["date"]

        if date_str < analysis_start:
            continue

        # Ventana de velas hasta hoy (inclusive)
        window = all_candles[max(0, i - WARMUP_CANDLES):i + 1]

        mhs_data = compute_mhs_daily(window)
        dbs_data = compute_dbs_daily(window)
        pip      = compute_pip(dbs_data["score"])
        trend    = daily_trend(window)

        mhs   = mhs_data["score"]
        zone  = mhs_data["zone"]
        dbs   = dbs_data["score"]
        direc = dbs_data["direction"]
        agr   = dbs_data["agreement"]
        price = candle_today["c"]

        # Edge vs precio Polymarket asumido
        if direc == "LONG":
            mkt_p = assumed_market_price
            side  = "YES"
        elif direc == "SHORT":
            mkt_p = 1 - assumed_market_price
            side  = "NO"
        else:
            mkt_p = assumed_market_price
            side  = "?"

        edge = round(pip - mkt_p, 3)

        # ── Gestión de posición abierta ──────────────────────────────────────
        closed_this_bar = False
        if open_pos:
            pos_side   = open_pos["side"]
            entry_p    = open_pos["entry_btc_price"]
            entry_date = open_pos["date"]
            entry_pip_val = open_pos["pip"]
            shares     = open_pos["shares"]
            usdc_in    = open_pos["usdc"]
            sl         = open_pos["sl"]
            tp         = open_pos["tp"]

            # ¿Precio de vela cruzó SL o TP?
            pnl = None; reason = None
            if price <= entry_p * sl:
                exit_p = entry_p * sl
                pnl    = (exit_p - entry_p) * shares if pos_side == "YES" else (entry_p - exit_p) * shares
                reason = "STOP_LOSS"
            elif price >= entry_p * tp:
                exit_p = entry_p * tp
                pnl    = (exit_p - entry_p) * shares if pos_side == "YES" else (entry_p - exit_p) * shares
                reason = "TAKE_PROFIT"
            elif (pos_side == "YES" and direc == "SHORT") or (pos_side == "NO" and direc == "LONG"):
                exit_p = price
                pnl    = (exit_p - entry_p) * shares if pos_side == "YES" else (entry_p - exit_p) * shares
                reason = "SIGNAL_REVERSED"

            if reason:
                pnl_pct = round((pnl / usdc_in) * 100, 1) if usdc_in > 0 else 0
                sim_cap += usdc_in + pnl
                icon = "✅" if pnl >= 0 else "❌"
                if pnl >= 0: wins += 1
                else: losses += 1
                total_pnl += pnl
                print(f"{date_str:<12} ${price:>9,.0f} {mhs:>6.1f} {dbs:>+7.3f} {pip:>6.3f} "
                      f"{direc:<8} {edge:>+7.3f}  "
                      f"{icon} CIERRE {reason:<14}  PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)")
                open_pos = None
                closed_this_bar = True

        # ── Evaluar nueva entrada ────────────────────────────────────────────
        signal = "—"
        accion = ""

        if not open_pos and not closed_this_bar:
            blocks = []
            if mhs < mhs_min:
                blocks.append(f"MHS<{mhs_min:.0f}")
            if direc == "NEUTRAL":
                blocks.append("NEUTRAL")
            if edge < edge_min:
                blocks.append(f"Edge<{edge_min:.2f}")
            if agr < 3:
                blocks.append(f"agr={agr}<3")

            if not blocks:
                # ¡Entrada!
                size_usdc = kelly_size(pip, mkt_p, sim_cap)
                shares    = round(size_usdc / price, 6) if price > 0 else 0
                sl_mult   = 1 - 0.35   # stop-loss en -35% del precio BTC
                tp_mult   = 1 + 0.50   # take-profit en +50% del precio BTC

                open_pos = {
                    "date":      date_str,
                    "side":      side,
                    "entry_btc_price": price,
                    "pip":       pip,
                    "usdc":      size_usdc,
                    "shares":    shares,
                    "sl":        sl_mult,
                    "tp":        tp_mult,
                    "mhs":       mhs,
                    "dbs":       dbs,
                }
                sim_cap -= size_usdc
                signal = f"🚀 ENTRAR {side}"
                accion = f"${size_usdc:.2f} USDC | {shares:.4f} BTC"
                entries.append({
                    "date": date_str, "price": price,
                    "mhs": mhs, "dbs": dbs, "pip": pip,
                    "side": side, "edge": edge,
                    "size": size_usdc,
                })
                print(f"{date_str:<12} ${price:>9,.0f} {mhs:>6.1f} {dbs:>+7.3f} {pip:>6.3f} "
                      f"{direc:<8} {edge:>+7.3f}  "
                      f"{'🚀 ENTRADA ' + side:<20}  {accion}")
            else:
                # Solo imprimir si está cerca o es interesante
                reason_str = " | ".join(blocks)
                if mhs >= mhs_min * 0.85 and direc != "NEUTRAL":
                    print(f"{date_str:<12} ${price:>9,.0f} {mhs:>6.1f} {dbs:>+7.3f} {pip:>6.3f} "
                          f"{direc:<8} {edge:>+7.3f}  "
                          f"{'⛔ BLOQUEADO':<20}  [{reason_str}]")

    # ── Cerrar posición abierta al final ─────────────────────────────────────
    if open_pos:
        last = all_candles[-1]
        exit_p = last["c"]
        entry_p = open_pos["entry_btc_price"]
        shares  = open_pos["shares"]
        usdc_in = open_pos["usdc"]
        pnl     = (exit_p - entry_p) * shares if open_pos["side"] == "YES" else (entry_p - exit_p) * shares
        pnl_pct = round((pnl / usdc_in) * 100, 1) if usdc_in > 0 else 0
        sim_cap += usdc_in + pnl
        total_pnl += pnl
        if pnl >= 0: wins += 1
        else: losses += 1
        print(f"\n⚠  Posición abierta al final → cerrada en ${exit_p:,.0f}  PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%)")

    # ── Resumen ────────────────────────────────────────────────────────────────
    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0
    total_return_pct = ((sim_cap - capital) / capital) * 100

    print(f"\n{'='*72}")
    print(f"  RESUMEN DEL BACKTEST")
    print(f"{'='*72}")
    print(f"  Entradas totales:  {total_trades}")
    print(f"  Ganadas:           {wins}  |  Perdidas: {losses}")
    print(f"  Win rate:          {win_rate:.1%}")
    print(f"  PnL total:        ${total_pnl:+.2f} USDC")
    print(f"  Capital final:    ${sim_cap:.2f} USDC  (inicio: ${capital:.2f})")
    print(f"  Retorno total:    {total_return_pct:+.1f}%")
    print(f"{'='*72}")

    if not entries:
        print("\n  ⚠  No se generaron señales en el período.")
        print(f"     Intenta reducir --mhs_min (actual: {mhs_min}) o --edge (actual: {edge_min})")
        print(f"     Recuerda: en backtest no hay macro (FRED) ni sentimiento (LLM),")
        print(f"     así que el MHS real será más bajo. El bot en vivo usa MHS_MIN=75.")
    else:
        print(f"\n  📋 Detalle de entradas ({len(entries)}):")
        for e in entries:
            print(f"    {e['date']}  BTC ${e['price']:,.0f}  {e['side']}  "
                  f"MHS:{e['mhs']:.1f}  DBS:{e['dbs']:+.3f}  PIP:{e['pip']:.3f}  "
                  f"Edge:{e['edge']:+.3f}  Size:${e['size']:.2f}")

    print(f"\n  💡 Nota: Este backtest usa solo indicadores técnicos (sin FRED, sin LLM).")
    print(f"     En producción el MHS incluye macro (25%) y sentimiento (35%) →")
    print(f"     el bot puede ser más o menos selectivo dependiendo del contexto.")
    print()


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="tumbot — Backtest de señales BTC sobre datos históricos"
    )
    parser.add_argument("--ticker",   default="BTC-USD",   help="Ticker yfinance (default: BTC-USD)")
    parser.add_argument("--days",     type=int, default=180, help="Días hacia atrás (default: 180)")
    parser.add_argument("--start",    default=None,         help="Fecha inicio YYYY-MM-DD")
    parser.add_argument("--mhs_min",  type=float, default=60.0,  help="MHS mínimo para entrar (default: 60, prod: 75)")
    parser.add_argument("--edge",     type=float, default=0.08,  help="Edge mínimo vs precio Poly (default: 0.08)")
    parser.add_argument("--capital",  type=float, default=500.0, help="Capital inicial USDC (default: 500)")
    parser.add_argument("--mkt_price",type=float, default=0.50,  help="Precio Polymarket asumido (default: 0.50)")
    args = parser.parse_args()

    run_backtest(
        ticker=args.ticker,
        days=args.days,
        start=args.start,
        mhs_min=args.mhs_min,
        edge_min=args.edge,
        capital=args.capital,
        assumed_market_price=args.mkt_price,
    )
