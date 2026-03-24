"""
src/telegram/bot.py — Telegram interface layer for tumbot.
"""

import os
import threading
import logging
import functools
from datetime import datetime
from typing import Optional

from src.config import ET, WATCH_ASSETS
from src.data import database as DB

log = logging.getLogger("tumbot.telegram")

try:
    from telegram import (
        Update, BotCommand,
        ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    )
    from telegram.ext import (
        ApplicationBuilder, CommandHandler, ContextTypes, Application,
    )
    from telegram.constants import ParseMode
    HAS_TG = True
except ImportError:
    HAS_TG = False
    log.warning("python-telegram-bot not installed. Telegram disabled.")

_state:      Optional[dict]           = None
_lock:       Optional[threading.Lock] = None
_app:        Optional[object]         = None
_start_time: datetime                 = datetime.now(ET)
_paused      = threading.Event()

_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("📈 /signals"),   KeyboardButton("💼 /portfolio")],
        [KeyboardButton("📊 /positions"), KeyboardButton("📋 /trades")],
        [KeyboardButton("🤖 /status"),    KeyboardButton("⏸ /pause")],
        [KeyboardButton("▶️ /resume"),    KeyboardButton("❓ /help")],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Elige un comando ↓",
) if HAS_TG else None


def init(state: dict, lock: threading.Lock) -> None:
    global _state, _lock
    if not HAS_TG:
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    _state = state
    _lock  = lock
    threading.Thread(target=_run_polling, args=(token,), daemon=True).start()
    log.info("Telegram thread started.")


def _run_polling(token: str) -> None:
    import asyncio

    async def _main():
        global _app
        _app = ApplicationBuilder().token(token).build()
        for name, fn in [
            ("start",       cmd_start),
            ("vincular",    cmd_vincular),
            ("desvincular", cmd_desvincular),
            ("help",        cmd_help),
            ("positions",   cmd_positions),
            ("portfolio",   cmd_portfolio),
            ("signals",     cmd_signals),
            ("trades",      cmd_trades),
            ("status",      cmd_status),
            ("close",       cmd_close),
            ("pause",       cmd_pause),
            ("resume",      cmd_resume),
        ]:
            _app.add_handler(CommandHandler(name, fn))

        await _app.bot.set_my_commands([
            BotCommand("signals",     "Señales MHS · DBS · PIP por asset"),
            BotCommand("portfolio",   "Capital, drawdown, win-rate"),
            BotCommand("positions",   "Posiciones abiertas con SL · TP · PnL"),
            BotCommand("trades",      "Últimos 10 trades cerrados"),
            BotCommand("status",      "Estado del bot y uptime"),
            BotCommand("pause",       "Suspende nuevas entradas"),
            BotCommand("resume",      "Reactiva nuevas entradas"),
            BotCommand("close",       "Cierra una posición: /close BTC-USD"),
            BotCommand("help",        "Lista de comandos"),
            BotCommand("desvincular", "Libera el ownership del bot"),
        ])

        await _app.initialize()
        await _app.start()
        await _app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(3600)

    try:
        asyncio.run(_main())
    except Exception as exc:
        log.error(f"Telegram polling crashed: {exc}", exc_info=True)
        import traceback
        traceback.print_exc()


def _get_owner() -> Optional[str]:
    row = DB.get_bot_config("owner_chat_id")
    return row if row else None

def _is_owner(update: "Update") -> bool:
    owner = _get_owner()
    return bool(owner and str(update.effective_chat.id) == owner)

def _require_auth(handler):
    @functools.wraps(handler)
    async def wrapper(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        if not _is_owner(update):
            log.warning(f"Unauthorized — chat_id={update.effective_chat.id}")
            return
        return await handler(update, ctx)
    return wrapper

def _e(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text

def _fmt_pnl(pnl: float) -> str:
    return f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"

def _pnl_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(abs(pct) / 10 * width)))
    return "█" * filled + "░" * (width - filled)

def _uptime() -> str:
    delta = datetime.now(ET) - _start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

def _positions_snap() -> dict:
    if _state is None: return {}
    with _lock: return dict(_state.get("positions", {}))

def _poly_prices_snap() -> dict:
    if _state is None: return {}
    with _lock: return dict(_state.get("poly_prices", {}))


async def cmd_start(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    sender_id = str(update.effective_chat.id)
    owner     = _get_owner()
    if owner and owner == sender_id:
        await update.message.reply_text(
            "👋 *tumbot activo\\.*\n\nUsa el menú inferior o escribe un comando\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_MENU,
        )
        return
    if owner and owner != sender_id:
        return
    await update.message.reply_text(
        "🤖 *Bienvenido a tumbot*\n\n"
        "Este bot no tiene dueño aún\\.\n\n"
        "Para reclamarlo envía:\n\n"
        "`/vincular <frase_secreta>`\n\n"
        "La frase secreta es el valor de `TELEGRAM_LINK_SECRET` en el `\\.env` del servidor\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_vincular(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    sender_id = str(update.effective_chat.id)
    owner     = _get_owner()
    if owner and owner != sender_id:
        return
    if owner and owner == sender_id:
        await update.message.reply_text(
            "✅ Ya estás vinculado como dueño de este bot\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_MENU,
        )
        return
    secret_env = os.environ.get("TELEGRAM_LINK_SECRET", "").strip()
    if not secret_env:
        await update.message.reply_text(
            "⚠️ `TELEGRAM_LINK_SECRET` no configurado en el servidor\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    provided = " ".join(ctx.args).strip() if ctx.args else ""
    if provided != secret_env:
        log.warning(f"Wrong secret — chat_id={sender_id}")
        return
    DB.set_bot_config("owner_chat_id", sender_id)
    log.info(f"Bot claimed by chat_id={sender_id}")
    await update.message.reply_text(
        "🔐 *Bot vinculado correctamente\\.*\n\n"
        "Eres el único dueño de esta instancia\\.\n"
        "El menú de comandos ya está disponible abajo 👇",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=_MENU,
    )


async def cmd_desvincular(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    sender_id = str(update.effective_chat.id)
    owner     = _get_owner()
    if not owner or owner != sender_id:
        return
    DB.del_bot_config("owner_chat_id")
    await update.message.reply_text(
        "🔓 *Bot desvinculado\\.*\n\nUsa `/vincular <secreto>` para reclamarlo de nuevo\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=ReplyKeyboardRemove(),
    )


@_require_auth
async def cmd_help(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    txt = (
        "📊 *tumbot — Polymarket trading bot*\n\n"
        "*Posiciones y portafolio*\n"
        "  /positions — Posiciones abiertas con SL · TP · PnL\n"
        "  /portfolio — Capital, drawdown, win\\-rate\n"
        "  /trades    — Últimos 10 trades cerrados\n\n"
        "*Señales*\n"
        "  /signals   — MHS · DBS · PIP por asset\n\n"
        "*Control*\n"
        "  /close BTC\\-USD — Fuerza cierre de una posición\n"
        "  /pause     — Detiene nuevas entradas\n"
        "  /resume    — Reactiva entradas\n\n"
        "*Cuenta*\n"
        "  /desvincular — Libera este bot\n\n"
        "*Info*\n"
        "  /status    — Estado del bot y uptime\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_MENU)


@_require_auth
async def cmd_positions(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    positions   = _positions_snap()
    poly_prices = _poly_prices_snap()
    if not positions:
        await update.message.reply_text("📭 No hay posiciones abiertas ahora mismo\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    lines = [f"📈 *Posiciones Abiertas* \\({len(positions)} total\\)\n"]
    for asset, pos in positions.items():
        pp        = poly_prices.get(asset, {})
        cur_price = (pp.get("yes") if pos.side == "YES" else pp.get("no")) or pos.entry_price
        cur_val   = pos.shares * cur_price
        invested  = pos.usdc_spent
        pnl_usd   = cur_val - invested
        pnl_pct   = (pnl_usd / invested * 100) if invested else 0.0
        name      = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
        pnl_icon  = "🟢" if pnl_usd >= 0 else "🔴"
        arrow     = "▲" if pnl_usd >= 0 else "▼"
        side_icon = "🟢 YES" if pos.side == "YES" else "🔴 NO"
        entry_dt  = pos.entry_time.strftime("%b %d %H:%M") if hasattr(pos.entry_time, "strftime") else str(pos.entry_time)
        lines.append(
            f"*{name}* \\({_e(asset)}\\) — {side_icon}\n"
            f"  💰 {pos.shares:.2f} shares \\@ ${_e(f'{pos.entry_price:.3f}')} avg\n"
            f"  📥 ${_e(f'{invested:.2f}')} → ${_e(f'{cur_val:.2f}')}\n"
            f"  {pnl_icon} PnL: {_e(_fmt_pnl(pnl_usd))} USDC \\({arrow}{abs(pnl_pct):.1f}%\\) \\[{_pnl_bar(pnl_pct)}\\]\n"
            f"  🛑 SL: ${_e(f'{pos.stop_loss:.3f}')}   🎯 TP: ${_e(f'{pos.take_profit:.3f}')}\n"
            f"  🗓 {_e(entry_dt)} ET  MHS:{pos.entry_mhs:.0f}  DBS:{pos.entry_dbs:+.2f}  PIP:{pos.entry_pip:.3f}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_portfolio(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if _state is None:
        await update.message.reply_text("⚠️ Estado no disponible\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    with _lock:
        capital      = _state.get("capital_usdc", 0.0)
        peak         = _state.get("peak_capital", capital)
        positions    = _state.get("positions", {})
        bootstrap_ci = _state.get("bootstrap_ci")
    recent    = DB.load_recent_trades(limit=200)
    invested  = sum(p.usdc_spent for p in positions.values())
    total_val = capital + invested
    drawdown  = ((peak - total_val) / peak * 100) if peak else 0.0
    wins      = [t for t in recent if (t.get("pnl") or 0) > 0]
    losses    = [t for t in recent if (t.get("pnl") or 0) <= 0]
    win_rate  = (len(wins) / len(recent) * 100) if recent else 0.0
    total_pnl = sum(t.get("pnl", 0) for t in recent)
    dd_icon   = "🟢" if drawdown < 5 else ("🟡" if drawdown < 15 else "🔴")
    ci_txt = ""
    if bootstrap_ci:
        lo, hi = bootstrap_ci
        ci_txt = f"\n  Win CI 95%: {_e(f'{lo:.1%}')} – {_e(f'{hi:.1%}')}"
    txt = (
        f"💼 *Resumen del Portafolio*\n\n"
        f"  💵 Disponible: ${_e(f'{capital:.2f}')} USDC\n"
        f"  📊 Invertido:  ${_e(f'{invested:.2f}')} USDC\n"
        f"  🏦 Total:      ${_e(f'{total_val:.2f}')} USDC\n"
        f"  {dd_icon} Drawdown: {_e(f'{drawdown:.1f}')}% desde pico \\(${_e(f'{peak:.2f}')}\\)\n"
        f"  📈 PnL total: {_e(_fmt_pnl(total_pnl))} USDC\n\n"
        f"  🎯 Win rate: {_e(f'{win_rate:.1f}')}% \\({len(wins)}W / {len(losses)}L\\){ci_txt}\n"
        f"  🔁 Trades: {len(recent)}  📂 Pos abiertas: {len(positions)}\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_signals(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """Full signal dashboard using HTML parse mode — avoids MarkdownV2 escaping issues."""
    if _state is None:
        await update.message.reply_text("⚠️ Estado no disponible.", parse_mode=ParseMode.HTML)
        return

    with _lock:
        mhs_map   = dict(_state.get("mhs", {}))
        dbs_map   = dict(_state.get("dbs", {}))
        pip_map   = dict(_state.get("pip", {}))
        pip_v_map = dict(_state.get("pip_validated", {}))
        opps      = dict(_state.get("opportunities", {}))
        prices    = dict(_state.get("poly_prices", {}))
        lp        = dict(_state.get("last_price", {}))
        tf_map    = dict(_state.get("tf_trend", {}))
        macro     = _state.get("macro_data")
        sent      = _state.get("sentiment_data")

    pause_note = "⏸ <b>Motor PAUSADO</b>\n\n" if _paused.is_set() else ""
    lines = [f"🔭 <b>Dashboard de Señales</b>\n\n{pause_note}"]

    for asset in WATCH_ASSETS:
        mhs_d   = mhs_map.get(asset, {})
        dbs_d   = dbs_map.get(asset, {})
        pip_v   = pip_v_map.get(asset, {})
        pp      = prices.get(asset, {})
        mhs     = mhs_d.get("score", 0) if isinstance(mhs_d, dict) else 0
        dbs     = dbs_d.get("score", 0) if isinstance(dbs_d, dict) else 0
        pip     = pip_map.get(asset) or 0.0
        pip_adj = pip_v.get("adjusted_pip", pip) if pip_v else pip
        opp     = opps.get(asset)
        name    = WATCH_ASSETS[asset].get("name", asset)
        price   = lp.get(asset)
        yes_p   = pp.get("yes")
        no_p    = pp.get("no")
        tf      = tf_map.get(asset, "—")
        blocked = mhs_d.get("blocked", False) if isinstance(mhs_d, dict) else False
        bdown   = mhs_d.get("breakdown", {}) if isinstance(mhs_d, dict) else {}
        dir_lbl = dbs_d.get("direction", "NEUTRAL") if isinstance(dbs_d, dict) else "NEUTRAL"
        votes_raw = dbs_d.get("votes", 0) if isinstance(dbs_d, dict) else 0
        if isinstance(votes_raw, dict):
            votes = sum(1 for v in votes_raw.values() if v > 0)
        else:
            votes = int(votes_raw)
        dir_icon = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪"}.get(dir_lbl, "⚪")
        mhs_bar  = "█" * int(mhs // 10) + "░" * (10 - int(mhs // 10))

        price_txt = f"${price:,.2f}" if price else "—"
        t_s       = f"T:{bdown.get('tech',0):.0f} S:{bdown.get('sent',0):.0f} M:{bdown.get('macro',0):.0f}"
        yes_txt   = f"{yes_p:.3f}" if yes_p else "—"
        no_txt    = f"{no_p:.3f}"  if no_p  else "—"

        side_price = yes_p if dir_lbl == "LONG" else no_p
        edge       = round(pip - side_price, 3) if side_price else None
        edge_txt   = f"{edge:+.3f}" if edge is not None else "—"
        from src.config import MHS_MIN_DAILY
        has_edge = edge and edge >= 0.08
        has_mhs  = mhs >= MHS_MIN_DAILY
        if has_edge and has_mhs:
            edge_icon = "✅ listo para entrar"
        elif has_edge and not has_mhs:
            edge_icon = f"⚠️ edge OK pero MHS {mhs:.0f}&lt;{MHS_MIN_DAILY:.0f}"
        elif edge and edge > 0:
            edge_icon = "🔶 edge insuficiente"
        else:
            edge_icon = "❌ sin edge"

        pip_note = f" → adj {pip_adj:.3f}" if (pip_v and pip_v.get("valid")) else ""
        opp_txt  = f"\n  ⚡ <b>SEÑAL ACTIVA</b> — Edge {opp.get('edge',0):+.3f}" if opp else ""
        block_txt = "\n  🚫 VIX block activo" if blocked else ""

        lines.append(
            f"<b>{name}</b> ({asset})  {price_txt}\n"
            f"  MHS: {mhs:.0f}/100 [{mhs_bar}]  ({t_s})\n"
            f"  DBS: {dbs:+.2f} {dir_icon} {dir_lbl}  Votes: {votes}/4\n"
            f"  PIP: {pip:.3f}{pip_note}   Trend: {tf}\n"
            f"  YES: {yes_txt}   NO: {no_txt}\n"
            f"  Edge: {edge_txt} {edge_icon}\n"
            f"{block_txt}{opp_txt}\n"
        )

    macro_txt = ""
    if macro:
        vix_s = f"{macro.vix:.2f}" if macro.vix else "—"
        fed_s = f"{macro.fed_rate:.2f}%" if macro.fed_rate else "—"
        t10_s = f"{macro.t10y:.2f}%" if macro.t10y else "—"
        spr_s = f"{macro.spread:+.2f}%" if macro.spread is not None else "—"
        macro_txt = f"\n🌡 <b>Macro</b>\n  VIX: {vix_s}   Fed: {fed_s}   T10Y: {t10_s}   Spread: {spr_s}\n"

    sent_txt = ""
    if sent:
        sent_txt = (
            f"\n📰 <b>Sentimiento</b>\n"
            f"  NLP: {sent.score:+.2f}   F&G: {sent.fear_greed}/100   Bias: {sent.direction_bias}\n"
        )

    full = "\n".join(lines) + macro_txt + sent_txt
    if len(full) > 4000:
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        if macro_txt or sent_txt:
            await update.message.reply_text(macro_txt + sent_txt, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(full, parse_mode=ParseMode.HTML)


@_require_auth
async def cmd_trades(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    recent = DB.load_recent_trades(limit=10)
    if not recent:
        await update.message.reply_text("📭 Aún no hay trades cerrados\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    lines = ["📋 *Últimos 10 Trades*\n"]
    for t in recent:
        pnl  = t.get("pnl", 0.0)
        icon = "🟢" if pnl >= 0 else "🔴"
        lines.append(
            f"{icon} *{_e(t.get('asset','?'))}* {t.get('side','?')} — "
            f"{_e(_fmt_pnl(pnl))} USDC \\({t.get('pnl_pct', 0):+.1f}%\\)\n"
            f"  {_e(t.get('reason','?'))}  ·  {_e((t.get('time') or '')[:16])}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_status(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    from src.config import LLM_BACKEND, LLM_MODEL
    if _state is None:
        await update.message.reply_text("⚠️ Estado no disponible\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    with _lock:
        fetching    = set(_state.get("fetching", set()))
        last_update = _state.get("last_update", "—")
        last_signal = _state.get("last_signal", "—")
        status_msg  = _state.get("status", "—")
        capital     = _state.get("capital_usdc", 0.0)
        n_pos       = len(_state.get("positions", {}))
    try:
        from main import LIVE_MODE
    except Exception:
        LIVE_MODE = False
    txt = (
        f"🤖 *Estado de tumbot*\n\n"
        f"  Modo:    {'🔴 LIVE' if LIVE_MODE else '🟡 PAPER'}\n"
        f"  Motor:   {'⏸ PAUSADO' if _paused.is_set() else '▶️ CORRIENDO'}\n"
        f"  Uptime:  {_e(_uptime())}\n"
        f"  Capital: ${_e(f'{capital:.2f}')} USDC\n"
        f"  Pos:     {n_pos} abiertas\n\n"
        f"  Último dato:   {_e(last_update)}\n"
        f"  Última señal:  {_e(last_signal)}\n"
        f"  Fetching:      {_e(', '.join(fetching) if fetching else 'idle')}\n\n"
        f"  LLM: {_e(LLM_BACKEND)}/{_e(LLM_MODEL.split('/')[-1])}\n"
        f"  ℹ️ {_e(status_msg)}\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_close(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if not ctx.args:
        await update.message.reply_text("Uso: `/close BTC-USD`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    asset = ctx.args[0].upper()
    if _state is None:
        await update.message.reply_text("⚠️ Estado no disponible\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    with _lock:
        pos = _state.get("positions", {}).get(asset)
    if not pos:
        await update.message.reply_text(f"❌ No hay posición abierta para `{_e(asset)}`\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    with _lock:
        if "force_close" not in _state:
            _state["force_close"] = set()
        _state["force_close"].add(asset)
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    await update.message.reply_text(
        f"⚠️ *Cierre forzado encolado para {name}* \\({_e(asset)}\\)\n\n"
        f"  {pos.side}  |  {pos.shares:.2f} shares  |  Entry ${_e(f'{pos.entry_price:.3f}')}\n\n"
        f"Se ejecutará en el próximo tick \\(máx 60s\\)\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@_require_auth
async def cmd_pause(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if _paused.is_set():
        await update.message.reply_text("⏸ El motor ya está pausado\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    _paused.set()
    if _state is not None:
        with _lock: _state["engine_paused"] = True
    await update.message.reply_text(
        "⏸ *Motor PAUSADO\\.*\n\nNo se abrirán nuevas posiciones\\.\nUsa /resume para reactivar\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@_require_auth
async def cmd_resume(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _paused.is_set():
        await update.message.reply_text("▶️ El motor ya está corriendo\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    _paused.clear()
    if _state is not None:
        with _lock: _state["engine_paused"] = False
    await update.message.reply_text("▶️ *Motor REACTIVADO\\.*\nNuevas entradas habilitadas\\.", parse_mode=ParseMode.MARKDOWN_V2)


def _send_sync(text: str) -> None:
    if not HAS_TG or _app is None:
        return
    chat_id = _get_owner()
    if not chat_id:
        return
    def _push():
        import asyncio
        try:
            asyncio.run(_app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2))
        except Exception as exc:
            log.warning(f"Telegram push failed: {exc}")
    threading.Thread(target=_push, daemon=True).start()


def alert_position_opened(asset, side, shares, entry, usdc, sl, tp, mhs, dbs, pip):
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    _send_sync(
        f"🚀 *Posición Abierta*\n\n"
        f"  {'🟢' if side=='YES' else '🔴'} *{name}* \\({_e(asset)}\\) — {side}\n"
        f"  {shares:.2f} shares \\@ ${_e(f'{entry:.3f}')}\n"
        f"  Invertido: ${_e(f'{usdc:.2f}')} USDC\n\n"
        f"  🛑 SL: ${_e(f'{sl:.3f}')}   🎯 TP: ${_e(f'{tp:.3f}')}\n\n"
        f"  MHS:{mhs:.0f}  DBS:{dbs:+.2f}  PIP:{pip:.3f}"
    )

def alert_position_closed(asset, side, reason, pnl, pnl_pct, entry, exit_p):
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    _send_sync(
        f"{'🏆' if pnl >= 0 else '💸'} *Posición Cerrada* — {_e(reason)}\n\n"
        f"  *{name}* \\({_e(asset)}\\) {side}\n"
        f"  ${_e(f'{entry:.3f}')} → ${_e(f'{exit_p:.3f}')}\n"
        f"  PnL: {_e(_fmt_pnl(pnl))} USDC \\({pnl_pct:+.1f}%\\)"
    )

def alert_stop_loss(asset, side, trigger, pnl):
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    _send_sync(
        f"🛑 *Stop\\-Loss Ejecutado*\n\n"
        f"  *{name}* \\({_e(asset)}\\) {side}\n"
        f"  Precio: ${_e(f'{trigger:.3f}')}\n"
        f"  Pérdida: {_e(_fmt_pnl(pnl))} USDC"
    )

def alert_take_profit(asset, side, trigger, pnl):
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    _send_sync(
        f"🎯 *Take\\-Profit Alcanzado\\!*\n\n"
        f"  *{name}* \\({_e(asset)}\\) {side}\n"
        f"  Precio: ${_e(f'{trigger:.3f}')}\n"
        f"  Ganancia: {_e(_fmt_pnl(pnl))} USDC"
    )

def is_paused() -> bool:
    return _paused.is_set()