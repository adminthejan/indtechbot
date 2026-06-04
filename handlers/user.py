"""
User-facing Telegram handlers.
"""

from __future__ import annotations
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

from database import db
from binance_engine import BinanceFutures
from config import (
    ADMIN_IDS, SCAN_SYMBOLS, SCAN_TIMEFRAME, BINANCE_TESTNET,
    DEFAULT_LEVERAGE, DEFAULT_POSITION_SIZE,
)

logger = logging.getLogger(__name__)


# ── Keyboards ──────────────────────────────────────────────────────────────────

def main_kb(is_admin: bool = False):
    rows = [
        [InlineKeyboardButton("📊 Portfolio",    callback_data="portfolio"),
         InlineKeyboardButton("📋 My Trades",   callback_data="my_trades")],
        [InlineKeyboardButton("📡 Signals",      callback_data="signals"),
         InlineKeyboardButton("⚙️ Settings",    callback_data="settings")],
        [InlineKeyboardButton("🤖 Auto Trade",  callback_data="auto_toggle"),
         InlineKeyboardButton("❓ Help",         callback_data="help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="adm_main")])
    return InlineKeyboardMarkup(rows)


def settings_kb(user: dict):
    auto = "✅ Auto Trade ON" if user.get("auto_trade") else "❌ Auto Trade OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 API Keys",      callback_data="set_api")],
        [InlineKeyboardButton("⚡ Leverage",       callback_data="set_lev"),
         InlineKeyboardButton("📏 Position Size", callback_data="set_pos")],
        [InlineKeyboardButton("🎯 TP R:R Ratio",  callback_data="set_tprr"),
         InlineKeyboardButton("📐 DCA Method",    callback_data="set_dca")],
        [InlineKeyboardButton(auto,               callback_data="auto_toggle")],
        [InlineKeyboardButton("◀️ Back",           callback_data="main_menu")],
    ])


def dca_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧠 SMC (FVG / Liquidity)", callback_data="dca_FVG")],
        [InlineKeyboardButton("📐 Fibonacci 61.8%",       callback_data="dca_FIB618")],
        [InlineKeyboardButton("⚖️ 50% Midpoint",          callback_data="dca_MID50")],
        [InlineKeyboardButton("◀️ Back",                   callback_data="settings")],
    ])


def back_kb(dest: str = "main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=dest)]])


# ── /start ─────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await db.upsert_user(u.id, u.username, u.full_name)
    dbu = await db.get_user(u.id)
    role = dbu["role"] if dbu else "pending"

    if role == "banned":
        await update.message.reply_text("⛔ Your account is banned.")
        return

    if role == "pending":
        await update.message.reply_text(
            "👋 Welcome to *IndTech TradeX Pro Bot!*\n\n"
            "⏳ Your account is *pending admin approval*.\n"
            "You'll receive a message once approved.",
            parse_mode="Markdown"
        )
        for aid in ADMIN_IDS:
            try:
                await ctx.bot.send_message(aid,
                    f"🆕 *New user pending approval*\n"
                    f"Name    : `{u.full_name}`\n"
                    f"Username: `@{u.username}`\n"
                    f"ID      : `{u.id}`\n\n"
                    f"Use /admin to manage.",
                    parse_mode="Markdown")
            except Exception:
                pass
        return

    if not await db.is_active(u.id):
        await update.message.reply_text(
            "⏰ Your *subscription has expired*.\n"
            "Contact an admin to renew.",
            parse_mode="Markdown"
        )
        return

    is_adm = role == "admin"
    symbols_preview = ", ".join(SCAN_SYMBOLS[:4]) + "..."
    await update.message.reply_text(
        f"👋 Welcome back, *{u.first_name}*!\n\n"
        f"🤖 *IndTech TradeX Pro*\n"
        f"Binance Futures Auto-Trading Bot\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Symbols   : `{symbols_preview}`\n"
        f"⏱ Timeframe : `{SCAN_TIMEFRAME}`\n"
        f"🔄 Exits     : TSL + Supertrend Flip + TP/SL",
        parse_mode="Markdown",
        reply_markup=main_kb(is_admin=is_adm)
    )


# ── Callback dispatcher ────────────────────────────────────────────────────────

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = q.from_user.id
    dbu  = await db.get_user(uid)

    if not dbu or dbu["role"] in ("pending", "banned"):
        await q.edit_message_text("⛔ Access denied.")
        return

    is_adm = dbu["role"] == "admin"
    if not await db.is_active(uid) and not is_adm:
        await q.edit_message_text("⏰ Subscription expired.")
        return

    # ── Navigation ─────────────────────────────────────────────────────────────
    if data == "main_menu":
        await q.edit_message_text("🏠 *Main Menu*",
            parse_mode="Markdown", reply_markup=main_kb(is_admin=is_adm))

    elif data == "settings":
        await q.edit_message_text(
            "⚙️ *Settings*\nCustomize your trading configuration:",
            parse_mode="Markdown", reply_markup=settings_kb(dbu))

    elif data == "set_dca":
        await q.edit_message_text(
            "📐 *DCA Entry Method*\n\nChoose how the DCA price is calculated:",
            parse_mode="Markdown", reply_markup=dca_kb())

    elif data.startswith("dca_"):
        method = data.split("_", 1)[1]
        labels = {"FVG":"SMC FVG/Liquidity","FIB618":"Fibonacci 61.8%","MID50":"50% Midpoint"}
        await db.update_settings(uid, dca_method=method)
        await q.edit_message_text(
            f"✅ DCA method → *{labels.get(method, method)}*",
            parse_mode="Markdown", reply_markup=back_kb("settings"))

    # ── API Keys ───────────────────────────────────────────────────────────────
    elif data == "set_api":
        ctx.user_data["api_step"] = "key"
        has = "✅ Keys saved" if dbu.get("binance_key") else "❌ Not set"
        await q.edit_message_text(
            f"🔑 *Binance API Keys*\n\nCurrent status: {has}\n\n"
            "Send your *Binance Futures API Key*:\n\n"
            "⚠️ Key requirements:\n"
            "• Futures trading ✅\n"
            "• Withdrawals ❌ (disabled!)\n"
            "• IP whitelist = your server IP",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings")
            ]])
        )

    # ── Auto trade toggle ──────────────────────────────────────────────────────
    elif data == "auto_toggle":
        new = 0 if dbu.get("auto_trade") else 1
        await db.update_settings(uid, auto_trade=new)
        status = "✅ *ON*" if new else "❌ *OFF*"
        desc = (
            "Signals will automatically execute trades on your Binance Futures account."
            if new else
            "You'll receive signal alerts only — no trades will be placed automatically."
        )
        await q.edit_message_text(
            f"🤖 Auto Trading is now {status}\n\n{desc}",
            parse_mode="Markdown",
            reply_markup=back_kb("settings"))

    # ── Awaiting text inputs ───────────────────────────────────────────────────
    elif data == "set_lev":
        ctx.user_data["awaiting"] = "lev"
        await q.edit_message_text(
            f"⚡ *Leverage*\n\nCurrent: `{dbu.get('leverage', DEFAULT_LEVERAGE)}x`\n\nEnter value (1–125):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings")
            ]]))

    elif data == "set_pos":
        ctx.user_data["awaiting"] = "pos"
        await q.edit_message_text(
            f"📏 *Position Size*\n\nCurrent: `{dbu.get('position_size', DEFAULT_POSITION_SIZE)}%`\n\nEnter % of balance per trade (0.1–100):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings")
            ]]))

    elif data == "set_tprr":
        ctx.user_data["awaiting"] = "tprr"
        await q.edit_message_text(
            f"🎯 *Take Profit R:R Ratio*\n\nCurrent: `1:{dbu.get('tp_rr') or 2.0}`\n\nEnter ratio (e.g. `2.0` means 1:2):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="settings")
            ]]))

    # ── Portfolio ──────────────────────────────────────────────────────────────
    elif data == "portfolio":
        await show_portfolio(q, dbu)

    # ── My trades ──────────────────────────────────────────────────────────────
    elif data == "my_trades":
        await show_trades(q, uid)

    # ── Signals ────────────────────────────────────────────────────────────────
    elif data == "signals":
        await show_signals(q)

    # ── Help ───────────────────────────────────────────────────────────────────
    elif data == "help":
        await q.edit_message_text(
            "❓ *IndTech TradeX Pro — How it works*\n\n"
            "*Strategy Engine:*\n"
            "• Supertrend (ATR) — entry signal\n"
            "• Safe SL — SMC swing point protection\n"
            "• Smart DCA — FVG / Fibonacci / Midpoint\n"
            "• MTF Confirmation — 5m/15m/1h/4h/1D\n"
            "• Trailing SL — moves automatically\n\n"
            "*Exit Logic (all 4 active):*\n"
            "1. 🎯 TP hit — Binance stops out at profit\n"
            "2. 🛑 SL hit — Binance stops out at loss\n"
            "3. 🔒 TSL — trailing SL moves with price\n"
            "4. 🔄 ST Flip — exits when trend reverses\n\n"
            "*Your Steps:*\n"
            "1. Set API keys in ⚙️ Settings\n"
            "2. Enable 🤖 Auto Trade\n"
            "3. Bot handles everything else\n\n"
            "⚠️ Max 1–5% margin per trade. Always risk what you can afford to lose.",
            parse_mode="Markdown", reply_markup=back_kb())


# ── Portfolio ──────────────────────────────────────────────────────────────────

async def show_portfolio(q, dbu: dict):
    key = dbu.get("binance_key", "")
    sec = dbu.get("binance_secret", "")
    if not key:
        await q.edit_message_text(
            "⚙️ Binance API keys not set.\nGo to ⚙️ Settings → 🔑 API Keys",
            reply_markup=back_kb())
        return
    try:
        eng  = BinanceFutures(key, sec, testnet=BINANCE_TESTNET)
        bal  = eng.get_balance()
        pos  = eng.get_positions()
        b    = float(bal.get("balance", 0))
        av   = float(bal.get("withdrawAvailable", 0))

        text = (
            f"📊 *Portfolio*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 Balance   : `${b:.2f} USDT`\n"
            f"✅ Available : `${av:.2f} USDT`\n\n"
        )
        if pos:
            text += f"📈 *Open Positions ({len(pos)}):*\n"
            for p in pos[:8]:
                amt  = float(p["positionAmt"])
                pnl  = float(p["unRealizedProfit"])
                icon = "🟢" if amt > 0 else "🔴"
                pi   = "📈" if pnl >= 0 else "📉"
                text += f"{icon} `{p['symbol']}` | {pi} `${pnl:+.2f}`\n"
        else:
            text += "📭 No open positions."

        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb())
    except Exception as e:
        await q.edit_message_text(f"❌ Error:\n`{e}`",
            parse_mode="Markdown", reply_markup=back_kb())


# ── My Trades ──────────────────────────────────────────────────────────────────

async def show_trades(q, uid: int):
    trades = await db.get_recent_trades(uid, limit=5)
    open_t = await db.get_open_trades(uid)

    if not trades and not open_t:
        await q.edit_message_text("📭 No trades yet.", reply_markup=back_kb())
        return

    text = ""
    if open_t:
        text += f"🔓 *Open Trades ({len(open_t)})*\n"
        for t in open_t:
            icon = "🟢" if t["side"] == "BUY" else "🔴"
            tsl  = f"TSL `{t['current_tsl']}`" if t.get("current_tsl") else ""
            text += (
                f"{icon} `{t['symbol']}` | Entry `{t['entry_price']}`\n"
                f"   SL `{t['safe_sl']}` | TP `{t['tp']}` {tsl}\n"
                f"   {t['opened_at'][:16]} UTC\n\n"
            )

    if trades:
        text += f"📋 *Recent Closed Trades*\n"
        for t in trades:
            if t["status"] == "open":
                continue
            icon   = "🟢" if (t.get("pnl") or 0) >= 0 else "🔴"
            reason = t.get("exit_reason", "?")
            text += (
                f"{icon} `{t['symbol']}` `{t['side']}` — {reason}\n"
                f"   PnL `${(t.get('pnl') or 0):+.4f}` (`{(t.get('pnl_pct') or 0):+.2f}%`)\n\n"
            )

    if not text:
        text = "📭 No trade history yet."

    await q.edit_message_text(text or "📭 No trades.", parse_mode="Markdown",
                               reply_markup=back_kb())


# ── Signals ────────────────────────────────────────────────────────────────────

async def show_signals(q):
    sigs = await db.recent_signals(limit=3)
    if not sigs:
        await q.edit_message_text(
            "📡 No signals yet.\nScanner runs every few minutes.",
            reply_markup=back_kb())
        return
    text = "📡 *Recent Signals (last 3)*\n━━━━━━━━━━━━━━━━━━━━\n"
    for s in sigs:
        icon = "🟢" if s["side"] == "BUY" else "🔴"
        text += (
            f"{icon} `{s['symbol']}` `{s['timeframe']}` — *{s['side']}*\n"
            f"   Entry `{s['entry_price']}` | TP `{s['tp']}` | SL `{s['safe_sl']}`\n"
            f"   MTF `{s['mtf_score']}/5` | {s['created_at'][:16]} UTC\n\n"
        )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=back_kb())


# ── Text message handler ───────────────────────────────────────────────────────

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid      = update.effective_user.id
    text     = update.message.text.strip()
    awaiting = ctx.user_data.get("awaiting")
    api_step = ctx.user_data.get("api_step")
    dbu      = await db.get_user(uid)
    is_adm   = dbu and dbu.get("role") == "admin"

    # API key collection
    if api_step == "key":
        ctx.user_data["_key"] = text
        ctx.user_data["api_step"] = "secret"
        await update.message.reply_text(
            "🔒 Now send your *Binance API Secret*:",
            parse_mode="Markdown")
        return

    if api_step == "secret":
        key    = ctx.user_data.pop("_key", "")
        secret = text
        ctx.user_data.pop("api_step", None)
        await db.update_keys(uid, key, secret)
        try:
            await update.message.delete()  # delete message containing secret
        except Exception:
            pass
        await update.message.reply_text(
            "✅ *API keys saved!*\n"
            "🔒 Your secret message was deleted for security.",
            parse_mode="Markdown", reply_markup=main_kb(is_admin=is_adm))
        return

    # Leverage
    if awaiting == "lev":
        ctx.user_data.pop("awaiting")
        try:
            v = int(text)
            assert 1 <= v <= 125
            await db.update_settings(uid, leverage=v)
            await update.message.reply_text(
                f"✅ Leverage set to `{v}x`",
                parse_mode="Markdown", reply_markup=main_kb(is_admin=is_adm))
        except Exception:
            await update.message.reply_text(
                "❌ Invalid. Enter 1–125.",
                reply_markup=main_kb(is_admin=is_adm))
        return

    # Position size
    if awaiting == "pos":
        ctx.user_data.pop("awaiting")
        try:
            v = float(text)
            assert 0.1 <= v <= 100
            await db.update_settings(uid, position_size=v)
            await update.message.reply_text(
                f"✅ Position size set to `{v}%`",
                parse_mode="Markdown", reply_markup=main_kb(is_admin=is_adm))
        except Exception:
            await update.message.reply_text(
                "❌ Invalid. Enter 0.1–100.",
                reply_markup=main_kb(is_admin=is_adm))
        return

    # TP R:R
    if awaiting == "tprr":
        ctx.user_data.pop("awaiting")
        try:
            v = float(text)
            assert 0.1 <= v <= 50
            await db.update_settings(uid, tp_rr=v)
            await update.message.reply_text(
                f"✅ TP R:R set to `1:{v}`",
                parse_mode="Markdown", reply_markup=main_kb(is_admin=is_adm))
        except Exception:
            await update.message.reply_text(
                "❌ Invalid. Enter 0.1–50.",
                reply_markup=main_kb(is_admin=is_adm))
        return

    # Default
    await update.message.reply_text("Use the menu:", reply_markup=main_kb(is_admin=is_adm))


# ── Registration ────────────────────────────────────────────────────────────────

def register_user_handlers(app: Application):
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
