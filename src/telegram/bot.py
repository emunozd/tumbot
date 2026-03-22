"""
src/telegram/bot.py — Telegram interface layer for tumbot.

Corre como hilo daemon dentro del mismo proceso que tumbot.
Accede a `state` y `lock` directamente en memoria.
Persiste el owner_chat_id en la tabla bot_config de la misma DB.

Seguridad (dos capas):
  1. El bot es invisible en Telegram (configurado vía BotFather).
  2. /vincular <secreto> reclama ownership usando TELEGRAM_LINK_SECRET del .env.
     Cualquier chat_id no autorizado recibe silencio absoluto.

Comandos disponibles (solo para el dueño vinculado):
  /vincular <secreto>  — reclama el bot (solo si no hay dueño)
  /desvincular         — libera el ownership del dueño actual
  /positions           — posiciones abiertas con SL, TP, PnL
  /portfolio           — resumen de capital y rendimiento
  /signals             — señales MHS/DBS/PIP por asset
  /trades              — últimos 10 trades cerrados
  /status              — salud del bot, uptime, fetches activos
  /close <ASSET>       — fuerza cierre de una posición (ej. /close BTC-USD)
  /pause               — suspende nuevas entradas
  /resume              — reactiva entradas
  /help                — lista de comandos
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
    from telegram import Update
    from telegram.ext import (
        ApplicationBuilder, CommandHandler, ContextTypes, Application,
    )
    from telegram.constants import ParseMode
    HAS_TG = True
except ImportError:
    HAS_TG = False
    log.warning("python-telegram-bot no instalado. Telegram desactivado.")


# ── Referencias inyectadas desde main.py ──────────────────────────────────
_state:      Optional[dict]             = None
_lock:       Optional[threading.Lock]   = None
_app:        Optional["Application"]    = None  # type: ignore[type-arg]
_start_time: datetime                   = datetime.now(ET)

# Pause flag — set = pausado, not set = corriendo
_paused = threading.Event()


# ══════════════════════════════════════════════════════════════════════════
# Init — llamado desde main.py una sola vez
# ══════════════════════════════════════════════════════════════════════════

def init(state: dict, lock: threading.Lock) -> None:
    """Inyecta estado compartido y lanza el hilo de polling."""
    global _state, _lock

    if not HAS_TG:
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.info("TELEGRAM_BOT_TOKEN no configurado — Telegram desactivado.")
        return

    _state = state
    _lock  = lock

    threading.Thread(target=_run_polling, args=(token,), daemon=True).start()
    log.info("Hilo de Telegram iniciado.")


def _run_polling(token: str) -> None:
    import asyncio

    async def _main():
        global _app
        _app = ApplicationBuilder().token(token).build()

        handlers = [
            ("vincular",    cmd_vincular),
            ("desvincular", cmd_desvincular),
            ("start",       cmd_help),
            ("help",        cmd_help),
            ("positions",   cmd_positions),
            ("portfolio",   cmd_portfolio),
            ("signals",     cmd_signals),
            ("trades",      cmd_trades),
            ("status",      cmd_status),
            ("close",       cmd_close),
            ("pause",       cmd_pause),
            ("resume",      cmd_resume),
        ]
        for name, fn in handlers:
            _app.add_handler(CommandHandler(name, fn))

        await _app.initialize()
        await _app.start()
        await _app.updater.start_polling(drop_pending_updates=True)

        while True:
            await asyncio.sleep(3600)

    asyncio.run(_main())


# ══════════════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════════════

def _get_owner() -> Optional[str]:
    """Lee el owner_chat_id desde bot_config en la DB."""
    row = DB.get_bot_config("owner_chat_id")
    return row if row else None


def _is_owner(update: "Update") -> bool:
    owner = _get_owner()
    if not owner:
        return False
    return str(update.effective_chat.id) == owner


def _require_auth(handler):
    """
    Decorador para todos los comandos excepto /vincular y /desvincular.
    Si el sender no es el dueño registrado → silencio absoluto.
    """
    @functools.wraps(handler)
    async def wrapper(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE"):
        if not _is_owner(update):
            log.warning(
                f"Acceso no autorizado — chat_id={update.effective_chat.id} "
                f"user=@{getattr(update.effective_user, 'username', '?')}"
            )
            return  # silencio total
        return await handler(update, ctx)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════
# Helpers de formato
# ══════════════════════════════════════════════════════════════════════════

def _e(text: str) -> str:
    """Escapa caracteres especiales para MarkdownV2."""
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
    if _state is None:
        return {}
    with _lock:
        return dict(_state.get("positions", {}))


def _poly_prices_snap() -> dict:
    if _state is None:
        return {}
    with _lock:
        return dict(_state.get("poly_prices", {}))


# ══════════════════════════════════════════════════════════════════════════
# /vincular y /desvincular  (sin @_require_auth — lógica propia)
# ══════════════════════════════════════════════════════════════════════════

async def cmd_vincular(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """
    /vincular <secreto>

    - Sin dueño + secreto correcto  → registra este chat_id como dueño.
    - Sin dueño + secreto incorrecto → silencio.
    - Ya hay dueño y es otro         → silencio.
    - Ya hay dueño y es el mismo     → confirma que ya está vinculado.
    """
    sender_id = str(update.effective_chat.id)
    owner     = _get_owner()

    # Ya hay dueño y es otro → silencio
    if owner and owner != sender_id:
        log.warning(f"Intento de /vincular rechazado — chat_id={sender_id}")
        return

    # Ya es el dueño → confirmar
    if owner and owner == sender_id:
        await update.message.reply_text(
            "✅ Ya estás vinculado como dueño de este bot\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # No hay dueño → validar secreto
    secret_env = os.environ.get("TELEGRAM_LINK_SECRET", "").strip()
    if not secret_env:
        await update.message.reply_text(
            "⚠️ `TELEGRAM_LINK_SECRET` no está configurado en el servidor\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    provided = " ".join(ctx.args).strip() if ctx.args else ""

    if provided != secret_env:
        # Secreto incorrecto → silencio (no revelar que existe un secreto)
        log.warning(f"Secreto incorrecto en /vincular — chat_id={sender_id}")
        return

    # Secreto correcto → registrar dueño en DB
    DB.set_bot_config("owner_chat_id", sender_id)
    log.info(f"Bot vinculado con chat_id={sender_id}")

    await update.message.reply_text(
        "🔐 *Bot vinculado correctamente\\.*\n\n"
        "Eres el único dueño de esta instancia\\.\n"
        "Usa /help para ver los comandos disponibles\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_desvincular(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """
    /desvincular

    Solo el dueño actual puede desvincular.
    Borra owner_chat_id de bot_config en la DB.
    """
    sender_id = str(update.effective_chat.id)
    owner     = _get_owner()

    # No es el dueño → silencio
    if not owner or owner != sender_id:
        return

    DB.del_bot_config("owner_chat_id")
    log.info(f"Bot desvinculado — chat_id={sender_id}")

    await update.message.reply_text(
        "🔓 *Bot desvinculado\\.*\n\n"
        "Esta instancia ya no tiene dueño\\.\n"
        "Usa `/vincular <secreto>` para reclamarla de nuevo\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ══════════════════════════════════════════════════════════════════════════
# Comandos principales (todos protegidos con @_require_auth)
# ══════════════════════════════════════════════════════════════════════════

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
        "  /help      — Este mensaje\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_positions(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    positions   = _positions_snap()
    poly_prices = _poly_prices_snap()

    if not positions:
        await update.message.reply_text(
            "📭 No hay posiciones abiertas ahora mismo\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = [f"📈 *Posiciones Abiertas* \\({len(positions)} total\\)\n"]

    for asset, pos in positions.items():
        pp        = poly_prices.get(asset, {})
        cur_price = (pp.get("yes") if pos.side == "YES" else pp.get("no")) or pos.entry_price
        cur_val   = pos.shares * cur_price
        invested  = pos.usdc_spent
        pnl_usd   = cur_val - invested
        pnl_pct   = (pnl_usd / invested * 100) if invested else 0.0
        bar       = _pnl_bar(pnl_pct)
        name      = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
        pnl_icon  = "🟢" if pnl_usd >= 0 else "🔴"
        arrow     = "▲" if pnl_usd >= 0 else "▼"
        side_icon = "🟢 YES" if pos.side == "YES" else "🔴 NO"
        entry_dt  = (
            pos.entry_time.strftime("%b %d %H:%M")
            if hasattr(pos.entry_time, "strftime")
            else str(pos.entry_time)
        )

        lines.append(
            f"*{name}* \\({_e(asset)}\\) — {side_icon}\n"
            f"  💰 {pos.shares:.2f} shares \\@ ${_e(f'{pos.entry_price:.3f}')} avg\n"
            f"  📥 ${_e(f'{invested:.2f}')} invertido → ${_e(f'{cur_val:.2f}')} ahora\n"
            f"  {pnl_icon} PnL: {_e(_fmt_pnl(pnl_usd))} USDC \\({arrow}{abs(pnl_pct):.1f}%\\) \\[{bar}\\]\n"
            f"  🛑 SL: ${_e(f'{pos.stop_loss:.3f}')}   🎯 TP: ${_e(f'{pos.take_profit:.3f}')}\n"
            f"  🗓 Abierta: {_e(entry_dt)} ET\n"
            f"  MHS: {pos.entry_mhs:.0f}  DBS: {pos.entry_dbs:+.2f}  PIP: {pos.entry_pip:.3f}\n"
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
        f"  🔁 Mercados operados: {len(recent)}\n"
        f"  📂 Posiciones abiertas: {len(positions)}\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_signals(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if _state is None:
        await update.message.reply_text("⚠️ Estado no disponible\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    with _lock:
        mhs_map = dict(_state.get("mhs", {}))
        dbs_map = dict(_state.get("dbs", {}))
        pip_map = dict(_state.get("pip", {}))
        opps    = dict(_state.get("opportunities", {}))

    pause_note = "⏸ *Motor PAUSADO — sin nuevas entradas*\n\n" if _paused.is_set() else ""
    lines = [f"🔭 *Motor de Señales*\n\n{pause_note}"]

    for asset in WATCH_ASSETS:
        mhs_d    = mhs_map.get(asset, {})
        dbs_d    = dbs_map.get(asset, {})
        mhs      = mhs_d.get("score", 0) if isinstance(mhs_d, dict) else 0
        dbs      = dbs_d.get("score", 0) if isinstance(dbs_d, dict) else 0
        pip      = pip_map.get(asset) or 0.0
        opp      = opps.get(asset)
        name     = _e(WATCH_ASSETS[asset].get("name", asset))
        blocked  = mhs_d.get("blocked", False) if isinstance(mhs_d, dict) else False
        dir_lbl  = dbs_d.get("direction", "NEUTRAL") if isinstance(dbs_d, dict) else "NEUTRAL"
        dir_icon = {"LONG": "🟢", "SHORT": "🔴", "NEUTRAL": "⚪"}.get(dir_lbl, "⚪")
        mhs_bar  = "█" * int(mhs // 10) + "░" * (10 - int(mhs // 10))

        block_txt = "  🚫 VIX block activo\n" if blocked else ""
        opp_txt = ""
        if opp:
            edge_val = opp.get("edge", 0)
            opp_txt  = f"  ⚡ *SEÑAL ACTIVA* — Edge {_e(f'{edge_val:+.3f}')}\n"

        lines.append(
            f"*{name}* \\({_e(asset)}\\)\n"
            f"  MHS: {mhs:.0f}/100 \\[{mhs_bar}\\]\n"
            f"  DBS: {_e(f'{dbs:+.2f}')}  {dir_icon} {dir_lbl}\n"
            f"  PIP: {_e(f'{pip:.3f}')}\n"
            f"{block_txt}{opp_txt}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_trades(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    recent = DB.load_recent_trades(limit=10)

    if not recent:
        await update.message.reply_text(
            "📭 Aún no hay trades cerrados\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = ["📋 *Últimos 10 Trades*\n"]
    for t in recent:
        pnl    = t.get("pnl", 0.0)
        icon   = "🟢" if pnl >= 0 else "🔴"
        reason = _e(t.get("reason", "?"))
        ts     = _e((t.get("time") or "")[:16])
        lines.append(
            f"{icon} *{_e(t.get('asset','?'))}* {t.get('side','?')} — "
            f"{_e(_fmt_pnl(pnl))} USDC \\({t.get('pnl_pct', 0):+.1f}%\\)\n"
            f"  {reason}  ·  {ts}\n"
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
        from main import LIVE_MODE  # type: ignore[import]
    except Exception:
        LIVE_MODE = False

    mode_icon = "🔴 LIVE" if LIVE_MODE else "🟡 PAPER"
    paused    = "⏸ PAUSADO" if _paused.is_set() else "▶️ CORRIENDO"
    fetch_txt = _e(", ".join(fetching) if fetching else "idle")

    txt = (
        f"🤖 *Estado de tumbot*\n\n"
        f"  Modo:    {mode_icon}\n"
        f"  Motor:   {paused}\n"
        f"  Uptime:  {_e(_uptime())}\n"
        f"  Capital: ${_e(f'{capital:.2f}')} USDC\n"
        f"  Pos:     {n_pos} abiertas\n\n"
        f"  Último dato:   {_e(last_update)}\n"
        f"  Última señal:  {_e(last_signal)}\n"
        f"  Fetching:      {fetch_txt}\n\n"
        f"  LLM: {_e(LLM_BACKEND)}/{_e(LLM_MODEL.split('/')[-1])}\n"
        f"  ℹ️ {_e(status_msg)}\n"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


@_require_auth
async def cmd_close(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if not ctx.args:
        await update.message.reply_text(
            "Uso: `/close BTC-USD`\n"
            "Ejecuta /positions para ver los tickers disponibles\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    asset = ctx.args[0].upper()

    if _state is None:
        await update.message.reply_text("⚠️ Estado no disponible\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    with _lock:
        pos = _state.get("positions", {}).get(asset)

    if not pos:
        await update.message.reply_text(
            f"❌ No hay posición abierta para `{_e(asset)}`\\.\n"
            f"Ejecuta /positions para ver las posiciones activas\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    with _lock:
        if "force_close" not in _state:
            _state["force_close"] = set()
        _state["force_close"].add(asset)

    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    await update.message.reply_text(
        f"⚠️ *Cierre forzado encolado para {name}* \\({_e(asset)}\\)\n\n"
        f"  Lado: {pos.side}  |  Shares: {pos.shares:.2f}  |  Entry: ${_e(f'{pos.entry_price:.3f}')}\n\n"
        f"Se ejecutará en el próximo tick del motor \\(máx 60s\\)\\.\n"
        f"Recibirás una alerta de confirmación cuando se complete\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@_require_auth
async def cmd_pause(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if _paused.is_set():
        await update.message.reply_text("⏸ El motor ya está pausado\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    _paused.set()
    if _state is not None:
        with _lock:
            _state["engine_paused"] = True
    await update.message.reply_text(
        "⏸ *Motor PAUSADO\\.*\n\n"
        "No se abrirán nuevas posiciones\\.\n"
        "Las posiciones abiertas siguen siendo monitoreadas\\.\n"
        "Usa /resume para reactivar\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@_require_auth
async def cmd_resume(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    if not _paused.is_set():
        await update.message.reply_text("▶️ El motor ya está corriendo\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    _paused.clear()
    if _state is not None:
        with _lock:
            _state["engine_paused"] = False
    await update.message.reply_text(
        "▶️ *Motor REACTIVADO\\.*\n"
        "Nuevas entradas habilitadas\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ══════════════════════════════════════════════════════════════════════════
# Alertas push — llamadas desde main.py / engine.py (código síncrono)
# ══════════════════════════════════════════════════════════════════════════

def _send_sync(text: str) -> None:
    """Envía una alerta al dueño. Fire-and-forget desde código síncrono."""
    if not HAS_TG or _app is None:
        return
    chat_id = _get_owner()
    if not chat_id:
        return

    def _push():
        import asyncio
        try:
            asyncio.run(_app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            ))
        except Exception as exc:
            log.warning(f"Telegram push falló: {exc}")

    threading.Thread(target=_push, daemon=True).start()


def alert_position_opened(asset: str, side: str, shares: float,
                           entry: float, usdc: float,
                           sl: float, tp: float,
                           mhs: float, dbs: float, pip: float) -> None:
    name      = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    side_icon = "🟢" if side == "YES" else "🔴"
    _send_sync(
        f"🚀 *Posición Abierta*\n\n"
        f"  {side_icon} *{name}* \\({_e(asset)}\\) — {side}\n"
        f"  Shares: {shares:.2f} \\@ ${_e(f'{entry:.3f}')}\n"
        f"  Invertido: ${_e(f'{usdc:.2f}')} USDC\n\n"
        f"  🛑 SL: ${_e(f'{sl:.3f}')}   🎯 TP: ${_e(f'{tp:.3f}')}\n\n"
        f"  MHS: {mhs:.0f}  DBS: {dbs:+.2f}  PIP: {pip:.3f}"
    )


def alert_position_closed(asset: str, side: str, reason: str,
                           pnl: float, pnl_pct: float,
                           entry: float, exit_p: float) -> None:
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    icon = "🏆" if pnl >= 0 else "💸"
    _send_sync(
        f"{icon} *Posición Cerrada* — {_e(reason)}\n\n"
        f"  *{name}* \\({_e(asset)}\\) {side}\n"
        f"  Entry: ${_e(f'{entry:.3f}')} → Exit: ${_e(f'{exit_p:.3f}')}\n"
        f"  PnL: {_e(_fmt_pnl(pnl))} USDC \\({pnl_pct:+.1f}%\\)"
    )


def alert_stop_loss(asset: str, side: str, trigger: float, pnl: float) -> None:
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    _send_sync(
        f"🛑 *Stop\\-Loss Ejecutado*\n\n"
        f"  *{name}* \\({_e(asset)}\\) {side}\n"
        f"  Precio de disparo: ${_e(f'{trigger:.3f}')}\n"
        f"  Pérdida: {_e(_fmt_pnl(pnl))} USDC"
    )


def alert_take_profit(asset: str, side: str, trigger: float, pnl: float) -> None:
    name = _e(WATCH_ASSETS.get(asset, {}).get("name", asset))
    _send_sync(
        f"🎯 *Take\\-Profit Alcanzado\\!*\n\n"
        f"  *{name}* \\({_e(asset)}\\) {side}\n"
        f"  Precio de disparo: ${_e(f'{trigger:.3f}')}\n"
        f"  Ganancia: {_e(_fmt_pnl(pnl))} USDC"
    )


def is_paused() -> bool:
    """Consultado desde main.py para omitir nuevas entradas."""
    return _paused.is_set()
