"""
ui/display.py — Rich terminal display.

Single Responsibility: render state to the terminal. No logic here.
All data flows in via the shared state dict — nothing is computed here.
"""

import math
from datetime import datetime
from typing import Optional

from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich import box

from src.config import WATCH_ASSETS, ET
from src.trading.engine import in_entry_window
from src.data import database as DB
from src.signals.stats import edge_verdict

console = Console(width=220)  # fixed width — avoids truncation in docker logs


# ── Color helpers ──────────────────────────────────────────────────────────

def _mhs_style(s: Optional[float]) -> str:
    if s is None:   return "dim"
    if s >= 75:     return "bold green"
    if s >= 55:     return "green"
    if s >= 40:     return "yellow"
    return "red"

def _dbs_style(s: Optional[float]) -> str:
    if s is None:   return "dim"
    if s >= 0.4:    return "bold green"
    if s >= 0.1:    return "cyan"
    if s >= -0.1:   return "yellow"
    if s >= -0.4:   return "orange3"
    return "bold red"

def _vix_style(v: Optional[float]) -> str:
    if v is None:   return "dim"
    if v >= 30:     return "bold red"
    if v >= 20:     return "orange3"
    return "green"

def _zone_style(z: str) -> str:
    return {
        "BULL_STRONG": "bold green", "BULL": "green",
        "NEUTRAL":     "yellow",     "BEAR": "orange3",
        "BEAR_STRONG": "bold red",   "BLOCKED": "bold red",
    }.get(z, "dim")

def _chg_text(d: Optional[float], dp: Optional[float]) -> Text:
    if d is None: return Text("—", style="dim")
    sign  = "+" if d >= 0 else ""
    arrow = "▲" if d >= 0 else "▼"
    col   = "green" if d >= 0 else "red"
    return Text(f"{arrow} {sign}{d:.2f} ({sign}{dp:.2f}%)", style=col)


# ── Header ─────────────────────────────────────────────────────────────────

def header_bar(state: dict, live_mode: bool) -> Panel:
    now = datetime.now(ET)
    wd  = now.weekday()
    mo  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    mc  = now.replace(hour=16, minute=0,  second=0, microsecond=0)

    if wd >= 5:        ms = Text("● CLOSED (weekend)", style="red")
    elif mo<=now<=mc:  ms = Text("● MARKET OPEN", style="bold green")
    else:              ms = Text("● CLOSED (after hours)", style="yellow")

    mode = Text(" [LIVE] ", style="bold green") if live_mode else \
           Text(" [PAPER] ", style="bold yellow")
    spin = Text("⟳ " + ", ".join(state["fetching"]) + "  ", style="yellow") \
           if state["fetching"] else Text("")

    t = Text()
    t.append("  TUMBOT  ", style="bold white")
    t.append_text(mode)
    t.append_text(ms)
    t.append(f"   {now.strftime('%a %d %b %Y  %H:%M:%S ET')}  ", style="dim")
    t.append_text(spin)
    return Panel(t, style="on #080818", box=box.SIMPLE)


# ── Asset panel ────────────────────────────────────────────────────────────

def asset_panel(asset: str, state: dict) -> Panel:
    from src.signals.indicators import technicals

    cfg   = WATCH_ASSETS[asset]
    mhs   = state["mhs"].get(asset, {})
    dbs   = state["dbs"].get(asset, {})
    opp   = state["opportunities"].get(asset)
    pos   = state["positions"].get(asset)
    pp    = state["poly_prices"].get(asset, {})
    price = state["last_price"].get(asset)
    pv    = state["pip_validated"].get(asset, {})
    ev_v  = state["ev"].get(asset)
    t     = technicals(state["candles_1h"].get(asset, []))

    mhs_val  = mhs.get("score")
    dbs_val  = dbs.get("score")
    dbs_dir  = dbs.get("direction", "—")
    mhs_zone = mhs.get("zone", "—")
    brk      = mhs.get("breakdown", {})
    pip_v    = state["pip"].get(asset)
    is_open  = in_entry_window(asset)

    border = "cyan"
    if pos:  border = "green" if pos.side == "YES" else "red"
    elif opp: border = "cyan"
    elif mhs.get("blocked"): border = "red"

    # Header row
    hdr = Text()
    hdr.append(f"{asset}  ", style=f"bold {border}")
    hdr.append(f"{cfg['name']}  ", style="dim")
    if price:
        hdr.append(f"${price:,.2f}", style="bold white")
    win_marker = Text(" ● OPEN" if is_open else " ○ CLOSED",
                      style="green" if is_open else "dim")
    hdr.append_text(win_marker)

    # Indicators row
    ta = Table.grid(padding=(0, 2))
    for _ in range(6): ta.add_column()
    rsi_v = t["rsi"]
    mh    = t["macd"]["hist"]
    ta.add_row(
        Text("RSI", style="dim"),
        Text(f"{rsi_v:.1f}" if rsi_v else "—",
             style="green" if rsi_v and rsi_v < 30 else
                   ("red" if rsi_v and rsi_v > 70 else "yellow")),
        Text("MACD H", style="dim"),
        Text(f"{mh:+.3f}" if mh is not None else "—",
             style="green" if mh and mh > 0 else "red"),
        Text("1D trend", style="dim"),
        Text({"up": "↑ Up", "down": "↓ Down", "neutral": "→ Flat"}.get(
             state["tf_trend"].get(asset,"neutral"), "—"),
             style="green" if state["tf_trend"].get(asset)=="up" else
                   ("red" if state["tf_trend"].get(asset)=="down" else "yellow")),
    )

    # Scores row
    sr = Table.grid(padding=(0,3))
    sr.add_column(ratio=1); sr.add_column(ratio=1); sr.add_column(ratio=1)

    c1 = Table.grid()
    c1.add_row(Text("MHS ", style="dim"),
               Text(f"{mhs_val:.1f}" if mhs_val else "—", style=_mhs_style(mhs_val)))
    c1.add_row(Text(mhs_zone, style=_zone_style(mhs_zone)))
    if brk:
        c1.add_row(Text(f"T:{brk.get('tech',0):.0f} "
                        f"S:{brk.get('sent',0):.0f} "
                        f"M:{brk.get('macro',0):.0f}", style="dim"))

    c2 = Table.grid()
    c2.add_row(Text("DBS ", style="dim"),
               Text(f"{dbs_val:+.2f}" if dbs_val is not None else "—",
                    style=_dbs_style(dbs_val)))
    c2.add_row(Text(dbs_dir, style="green" if dbs_dir=="LONG" else
                    ("red" if dbs_dir=="SHORT" else "yellow")))
    c2.add_row(Text(f"Votes {dbs.get('agreement',0)}/4", style="dim"))

    c3 = Table.grid()
    c3.add_row(Text("PIP   ", style="dim"),
               Text(f"{pip_v:.2%}" if pip_v else "—", style="cyan"))
    c3.add_row(Text("YES   ", style="dim"),
               Text(f"{pp.get('yes'):.3f}" if pp.get("yes") else "—"))
    c3.add_row(Text("NO    ", style="dim"),
               Text(f"{pp.get('no'):.3f}"  if pp.get("no")  else "—"))

    sr.add_row(c1, c2, c3)

    # Opportunity row
    opp_txt = Text("")
    if opp:
        opp_txt = Text()
        opp_txt.append("OPPORTUNITY  ", style="bold cyan")
        opp_txt.append(f"BUY {opp['side']}  ",
                        style=f"bold {'green' if opp['side']=='YES' else 'red'}")
        opp_txt.append(f"mkt={opp['mkt_price']:.3f}  pip={opp['pip']:.2%}  "
                        f"edge={opp['edge']:.2%}", style="white")
        if ev_v is not None:
            opp_txt.append(f"  EV={ev_v:+.3f}", style="green" if ev_v>0 else "red")
        if pv.get("reason"):
            opp_txt.append(f"\n  LLM ({pv.get('confidence','?')}): "
                           f"{pv['reason'][:60]}", style="dim")

    # Open position row
    pos_txt = Text("")
    if pos and price:
        pnl_now = ((price - pos.entry_price) if pos.side=="YES"
                   else (pos.entry_price - price)) * pos.shares
        pc = "green" if pnl_now >= 0 else "red"
        pos_txt = Text()
        pos_txt.append(f"▶ {pos.side}  ", style=f"bold {'green' if pos.side=='YES' else 'red'}")
        pos_txt.append(f"entry={pos.entry_price:.3f}  shares={pos.shares:.1f}  "
                        f"spent=${pos.usdc_spent:.2f}  ", style="dim")
        pos_txt.append(f"PnL {'+' if pnl_now>=0 else ''}{pnl_now:.2f}  ",
                        style=f"bold {pc}")
        pos_txt.append(f"SL={pos.stop_loss:.3f}  TP={pos.take_profit:.3f}",
                        style="dim")
        if pos.stop_loss > 0 and pos.entry_price > 0 and price:
            pct_to_sl = (price - pos.stop_loss) / price * 100
            pos_txt.append(f"  ({pct_to_sl:.1f}% to stop)", style="dim")

    return Panel(
        Group(hdr, Text(""), ta, Rule(style="dim"), sr, Text(""), opp_txt, pos_txt),
        border_style=border, padding=(1,2),
        title=f"[bold {border}]{asset}[/]  [dim]{cfg['resolution'].upper()}[/]",
        title_align="left",
    )


# ── Macro panel ────────────────────────────────────────────────────────────

def macro_panel(state: dict) -> Panel:
    macro = state.get("macro_data")
    sent  = state.get("sentiment_data")
    now   = datetime.now(ET)

    # Entry window status per asset
    win_txt = Text()
    for ast, cfg in WATCH_ASSETS.items():
        is_open = in_entry_window(ast)
        label   = "24/7" if cfg.get("asset_type") == "crypto" else "9:30–13:30 ET"
        win_txt.append(f"{'●' if is_open else '○'} {ast} ({label})  ",
                        style="bold green" if is_open else "dim")

    g = Table.grid(padding=(0,4), expand=True)
    g.add_column(ratio=1); g.add_column(ratio=1); g.add_column(ratio=1)

    # VIX column
    vix = macro.vix if macro else None
    left = Table.grid()
    left.add_row(Text("VIX", style="dim"),
                 Text(f"  {vix:.2f}" if vix else "  —", style=_vix_style(vix)))
    if vix:
        mood = ("Complacency","bold green") if vix<15 else \
               ("Calm","green") if vix<20 else \
               ("Concern","yellow") if vix<25 else \
               ("Fear","orange3") if vix<30 else ("PANIC — BLOCKED","bold red")
        left.add_row(Text(f"  {mood[0]}", style=mood[1]))

    # Rates column
    mid = Table.grid()
    if macro:
        mid.add_row(Text("Fed Funds", style="dim"),
                    Text(f"  {macro.fed_rate:.2f}%" if macro.fed_rate else "  —"))
        mid.add_row(Text("T-Note 10Y", style="dim"),
                    Text(f"  {macro.t10y:.2f}%"    if macro.t10y     else "  —"))
        if macro.spread is not None:
            mid.add_row(Text("Spread",style="dim"),
                        Text(f"  {macro.spread:+.2f}%",
                             style="green" if macro.spread>0 else "red"))

    # Sentiment column
    right = Table.grid()
    if sent:
        right.add_row(Text("NLP score",  style="dim"),
                      Text(f"  {sent.score:+.2f}", style="green" if sent.score>0 else "red"))
        right.add_row(Text("F&G",        style="dim"), Text(f"  {sent.fear_greed}/100"))
        right.add_row(Text("Bias",       style="dim"), Text(f"  {sent.direction_bias}",style="cyan"))

    g.add_row(left, mid, right)
    return Panel(Group(win_txt, Text(""), g),
                 title="[bold yellow]🌡  Macro · Sentiment[/]",
                 border_style="yellow", padding=(1,2))


# ── Portfolio panel ────────────────────────────────────────────────────────

def portfolio_panel(state: dict, live_mode: bool) -> Panel:
    capital = state["capital_usdc"]
    peak    = state["peak_capital"]
    trades  = state["trades"]

    stats = DB.trade_stats()
    total_pnl   = stats.get("total_pnl")  or 0
    winners     = stats.get("winners")    or 0
    total_count = stats.get("total")      or 0
    losers      = total_count - winners
    win_rate    = winners / total_count * 100 if total_count else 0.0
    total_lr    = stats.get("total_log_return") or 0
    total_lr_pct= (math.exp(total_lr) - 1) * 100 if total_lr else 0.0
    avg_ev      = stats.get("avg_ev") or 0.0

    mtm = 0.0
    for ast, pos in state["positions"].items():
        cur = state["last_price"].get(ast, pos.entry_price)
        mtm += ((cur - pos.entry_price) if pos.side=="YES"
                else (pos.entry_price - cur)) * pos.shares
    total_val = capital + mtm
    dd = (peak - total_val) / peak * 100 if peak > 0 else 0

    g = Table.grid(padding=(0,3))
    g.add_column(style="dim", width=16); g.add_column()
    g.add_column(style="dim", width=14); g.add_column()

    pc  = "green" if total_pnl >= 0 else "red"
    lrc = "green" if total_lr_pct >= 0 else "red"
    g.add_row("USDC capital",  Text(f"${capital:.2f}",    style="bold white"),
              "Total value",   Text(f"${total_val:.2f}",  style="bold cyan"))
    g.add_row("Realized PnL",  Text(f"${total_pnl:+.2f}", style=f"bold {pc}"),
              "Max drawdown",  Text(f"-{dd:.1f}%",         style="orange3" if dd>10 else "dim"))
    g.add_row("Log return",    Text(f"{total_lr_pct:+.2f}% (ln={total_lr:+.4f})",
                                     style=f"bold {lrc}"),
              "Avg EV/USD",    Text(f"{avg_ev:+.4f}",      style="cyan" if avg_ev>0 else "red"))
    g.add_row("Trades",        Text(str(total_count),      style="white"),
              "Win rate",      Text(f"{win_rate:.0f}%  ({winners}W / {losers}L)",
                                     style="green" if win_rate>=55 else "yellow"))

    ci            = state.get("bootstrap_ci")
    verdict_label, verdict_style = edge_verdict(ci)
    g.add_row("Edge (95% CI)", Text(verdict_label, style=verdict_style),
              "",              Text(""))

    tt = Table.grid(padding=(0,1))
    for _ in range(6): tt.add_column()
    for trade in list(reversed(trades))[:6]:
        col = "green" if trade.pnl > 0 else "red"
        tt.add_row(
            Text(trade.time, style="dim"),
            Text(trade.asset, style="dim"),
            Text(trade.side, style=col),
            Text(f"{trade.entry_price:.3f}→{trade.exit_price:.3f}", style="dim"),
            Text(f"${trade.pnl:+.2f}", style=f"bold {col}"),
            Text(trade.reason, style="dim"),
        )

    mode = Text()
    mode.append("● LIVE — Polymarket CLOB", style="bold green") if live_mode else \
    mode.append("● PAPER — simulation only", style="bold yellow")

    return Panel(
        Group(mode, Text(""), g, Text(""), Text("── Recent trades", style="dim"), tt),
        title="[bold magenta]💼  Portfolio[/]",
        border_style="magenta", padding=(1,2),
    )


# ── Status bar ─────────────────────────────────────────────────────────────

def status_bar(state: dict) -> Text:
    sig = state.get("last_signal", "—")
    upd = state.get("last_update", "—")
    cd  = state.get("countdown", 0)
    try:
        dbs  = DB.db_stats()
        db_info = (f"  DB: {dbs['trades']} trades · "
                   f"{dbs['candles_hist']} hist · "
                   f"{dbs['size_kb']:.0f}KB")
    except Exception:
        db_info = ""
    return Text(
        f"  Data:{upd}  Signal:{sig}  •  next in {cd}s{db_info}  •  Ctrl+C to quit",
        style="dim",
    )


# ── Full layout ────────────────────────────────────────────────────────────

def build_layout(state: dict, live_mode: bool):
    ag = Table.grid(expand=True, padding=(0,1))
    for _ in WATCH_ASSETS:
        ag.add_column(ratio=1)
    ag.add_row(*[asset_panel(a, state) for a in WATCH_ASSETS])

    root = Table.grid(expand=True)
    root.add_column()
    root.add_row(header_bar(state, live_mode))
    root.add_row(ag)
    root.add_row(macro_panel(state))
    root.add_row(portfolio_panel(state, live_mode))
    root.add_row(Rule(style="dim"))
    root.add_row(status_bar(state))
    return root