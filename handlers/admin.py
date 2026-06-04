"""Admin panel handlers."""

from __future__ import annotations
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from database import db

logger = logging.getLogger(__name__)

ROLE_ICON = {"pending":"⏳","user":"✅","admin":"👑","banned":"🚫"}


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ Pending",      callback_data="adm_list_pending"),
         InlineKeyboardButton("✅ Active",       callback_data="adm_list_user")],
        [InlineKeyboardButton("🚫 Banned",       callback_data="adm_list_banned"),
         InlineKeyboardButton("👥 All Users",    callback_data="adm_list_all")],
        [InlineKeyboardButton("📡 Signals",      callback_data="adm_signals"),
         InlineKeyboardButton("📈 Stats",        callback_data="adm_stats")],
        [InlineKeyboardButton("📣 Broadcast",    callback_data="adm_broadcast")],
    ])


async def _is_admin(uid: int) -> bool:
    u = await db.get_user(uid)
    return bool(u and u.get("role") == "admin")


async def admin_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "🛠 *Admin Panel*\n━━━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown", reply_markup=admin_kb())


async def admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    if not await _is_admin(uid):
        await q.edit_message_text("⛔ Admin only.")
        return

    # ── Back to main ──────────────────────────────────────────────────────────
    if data == "adm_main":
        await q.edit_message_text(
            "🛠 *Admin Panel*\n━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown", reply_markup=admin_kb())

    # ── User lists ─────────────────────────────────────────────────────────────
    elif data.startswith("adm_list_"):
        role = data.replace("adm_list_", "")
        role = None if role == "all" else role
        users = await db.list_users(role)
        if not users:
            await q.edit_message_text("📭 No users found.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="adm_main")
                ]]))
            return
        btns = []
        for u in users[:20]:
            name = (u.get("full_name") or u.get("username") or str(u["user_id"]))[:22]
            icon = ROLE_ICON.get(u["role"], "❓")
            btns.append([InlineKeyboardButton(
                f"{icon} {name} (@{u.get('username','?')})",
                callback_data=f"adm_user_{u['user_id']}"
            )])
        btns.append([InlineKeyboardButton("◀️ Back", callback_data="adm_main")])
        await q.edit_message_text(
            f"👥 *{(role or 'all').title()} Users* ({len(users)})",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(btns))

    # ── User detail ────────────────────────────────────────────────────────────
    elif data.startswith("adm_user_"):
        tid = int(data.split("_")[-1])
        await show_detail(q, ctx, tid)

    # ── Actions ────────────────────────────────────────────────────────────────
    elif data.startswith("adm_approve_"):
        tid = int(data.split("_")[-1])
        await db.set_subscription(tid, 30)
        await q.edit_message_text(f"✅ Approved `{tid}` — 30-day subscription granted.",
            parse_mode="Markdown")
        try:
            await ctx.bot.send_message(tid,
                "🎉 *Account Approved!*\n\n"
                "You now have a *30-day subscription*.\n"
                "Use /start to access the bot.",
                parse_mode="Markdown")
        except Exception: pass

    elif data.startswith("adm_ban_"):
        tid = int(data.split("_")[-1])
        await db.set_role(tid, "banned")
        await q.edit_message_text(f"🚫 User `{tid}` banned.", parse_mode="Markdown")
        try:
            await ctx.bot.send_message(tid, "⛔ Your account has been banned.")
        except Exception: pass

    elif data.startswith("adm_unban_"):
        tid = int(data.split("_")[-1])
        await db.set_role(tid, "pending")
        await q.edit_message_text(f"✅ User `{tid}` unbanned → pending.", parse_mode="Markdown")

    elif data.startswith("adm_mkadmin_"):
        tid = int(data.split("_")[-1])
        await db.set_role(tid, "admin")
        await q.edit_message_text(f"👑 User `{tid}` promoted to admin.", parse_mode="Markdown")

    elif data.startswith("adm_sub"):
        # adm_sub30_123456
        parts = data.split("_")   # ['adm', 'sub30', '123456']
        days  = int("".join(c for c in parts[1] if c.isdigit()))
        tid   = int(parts[2])
        await db.set_subscription(tid, days)
        await q.edit_message_text(
            f"✅ *{days}-day subscription* granted to `{tid}`.",
            parse_mode="Markdown")
        try:
            await ctx.bot.send_message(tid,
                f"✅ Your subscription has been extended by *{days} days*.",
                parse_mode="Markdown")
        except Exception: pass

    # ── Stats ──────────────────────────────────────────────────────────────────
    elif data == "adm_stats":
        all_u = await db.list_users()
        auto  = await db.get_auto_users()
        sigs  = await db.recent_signals(100)
        counts = {r: sum(1 for u in all_u if u["role"] == r)
                  for r in ("pending","user","admin","banned")}
        open_t = await db.get_open_trades()

        await q.edit_message_text(
            f"📈 *Bot Statistics*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users   : `{len(all_u)}`\n"
            f"⏳ Pending       : `{counts['pending']}`\n"
            f"✅ Active        : `{counts['user']}`\n"
            f"👑 Admins        : `{counts['admin']}`\n"
            f"🚫 Banned        : `{counts['banned']}`\n"
            f"🤖 Auto-trading  : `{len(auto)}`\n"
            f"📡 Total Signals : `{len(sigs)}`\n"
            f"🔓 Open Trades   : `{len(open_t)}`\n"
            f"\n⏰ `{datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="adm_main")
            ]]))

    # ── Last signals ───────────────────────────────────────────────────────────
    elif data == "adm_signals":
        sigs = await db.recent_signals(5)
        if not sigs:
            await q.edit_message_text("📡 No signals yet.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back", callback_data="adm_main")
                ]]))
            return
        text = "📡 *Last 5 Signals*\n━━━━━━━━━━━━━━━━━━━━\n"
        for s in sigs:
            icon = "🟢" if s["side"] == "BUY" else "🔴"
            text += (
                f"{icon} `{s['symbol']}` `{s['timeframe']}` — *{s['side']}*\n"
                f"   Entry `{s['entry_price']}` | TP `{s['tp']}` | SL `{s['safe_sl']}`\n"
                f"   MTF `{s['mtf_score']}/5` | {s['created_at'][:16]} UTC\n\n"
            )
        await q.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Back", callback_data="adm_main")
            ]]))

    # ── Broadcast ──────────────────────────────────────────────────────────────
    elif data == "adm_broadcast":
        ctx.user_data["adm_await"] = "broadcast"
        await q.edit_message_text(
            "📣 *Broadcast*\n\nSend the message to broadcast to all active users:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm_main")
            ]]))


async def show_detail(q, ctx, tid: int):
    u = await db.get_user(tid)
    if not u:
        await q.edit_message_text("❌ User not found.")
        return

    icon = ROLE_ICON.get(u["role"], "❓")
    sub  = (u.get("subscription_expiry") or "None")[:16]
    auto = "✅" if u.get("auto_trade") else "❌"
    keys = "✅" if u.get("binance_key") else "❌"
    open_t = await db.get_open_trades(tid)

    text = (
        f"👤 *User Detail*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Name      : `{u.get('full_name','?')}`\n"
        f"Username  : `@{u.get('username','?')}`\n"
        f"ID        : `{tid}`\n"
        f"Role      : {icon} `{u['role']}`\n"
        f"Sub Exp   : `{sub}`\n"
        f"Auto Trade: {auto}\n"
        f"API Keys  : {keys}\n"
        f"Leverage  : `{u.get('leverage',10)}x`\n"
        f"Pos Size  : `{u.get('position_size',5.0)}%`\n"
        f"Open Trades: `{len(open_t)}`\n"
        f"Joined    : `{(u.get('joined_at') or '')[:16]}`"
    )

    btns = [
        [InlineKeyboardButton("✅ Approve",      callback_data=f"adm_approve_{tid}"),
         InlineKeyboardButton("🚫 Ban",           callback_data=f"adm_ban_{tid}")],
        [InlineKeyboardButton("🔓 Unban",         callback_data=f"adm_unban_{tid}"),
         InlineKeyboardButton("👑 Make Admin",    callback_data=f"adm_mkadmin_{tid}")],
        [InlineKeyboardButton("📅 +7d",           callback_data=f"adm_sub7_{tid}"),
         InlineKeyboardButton("📅 +30d",          callback_data=f"adm_sub30_{tid}"),
         InlineKeyboardButton("📅 +90d",          callback_data=f"adm_sub90_{tid}")],
        [InlineKeyboardButton("◀️ Back",          callback_data="adm_list_all")],
    ]
    await q.edit_message_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(btns))


async def admin_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await _is_admin(uid):
        return
    if ctx.user_data.get("adm_await") == "broadcast":
        ctx.user_data.pop("adm_await")
        msg   = update.message.text
        users = await db.list_users("user")
        sent  = 0
        for u in users:
            try:
                await ctx.bot.send_message(u["user_id"],
                    f"📣 *IndTech TradeX — Announcement*\n\n{msg}",
                    parse_mode="Markdown")
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(
            f"✅ Broadcast sent to *{sent}/{len(users)}* users.",
            parse_mode="Markdown", reply_markup=admin_kb())


def register_admin_handlers(app: Application):
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^adm_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_msg), group=1)
