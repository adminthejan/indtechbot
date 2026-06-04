"""
Signal Scanner
==============
Runs every SCAN_INTERVAL_MINUTES.
For each symbol: runs TradeX strategy → if signal + MTF filter → execute.
"""

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime

from binance.client import Client
from telegram.ext import Application

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    SCAN_SYMBOLS, SCAN_TIMEFRAME, ADMIN_IDS, MAX_OPEN_TRADES,
    MTF_CONFIRM_MIN_SCORE,
    ST_ATR_PERIOD, ST_MULTIPLIER, SL_BUFFER_ATR, TSL_BUFFER_ATR,
    TP_RR_RATIO, TP_MODE, TP_PERCENT, DCA_METHOD, MTF_PIVOT_LEN,
    DEFAULT_LEVERAGE, DEFAULT_MARGIN_TYPE, DEFAULT_POSITION_SIZE,
)
from database import db
from strategy import TradeXEngine, Signal
from binance_engine import BinanceFutures

logger = logging.getLogger(__name__)


def _global_cfg() -> dict:
    return dict(
        st_atr_period  = ST_ATR_PERIOD,
        st_multiplier  = ST_MULTIPLIER,
        sl_buffer_atr  = SL_BUFFER_ATR,
        tsl_buffer_atr = TSL_BUFFER_ATR,
        tp_mode        = TP_MODE,
        tp_rr          = TP_RR_RATIO,
        tp_percent     = TP_PERCENT,
        dca_method     = DCA_METHOD,
        mtf_pivot_len  = MTF_PIVOT_LEN,
    )


def _user_cfg(user: dict) -> dict:
    cfg = _global_cfg()
    overrides = {
        "st_multiplier": user.get("st_multiplier"),
        "sl_buffer_atr": user.get("sl_buffer"),
        "tp_rr":         user.get("tp_rr"),
        "tp_mode":       user.get("tp_mode"),
        "dca_method":    user.get("dca_method"),
    }
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


async def _notify(app: Application, user_id: int, text: str):
    try:
        await app.bot.send_message(user_id, text, parse_mode="Markdown")
    except Exception as e:
        logger.warning("Notify %s: %s", user_id, e)


async def _execute_for_user(app: Application, user: dict,
                             signal: Signal, signal_id: int):
    """Open a trade on Binance for a single user."""
    user_id    = user["user_id"]
    api_key    = user.get("binance_key", "")
    api_secret = user.get("binance_secret", "")

    if not api_key or not api_secret:
        await _notify(app, user_id,
            "⚠️ *Signal received but API keys not set!*\n"
            "Go to ⚙️ Settings → 🔑 Set API Keys to start auto-trading.")
        return

    # Check existing open trades for this symbol (avoid duplicate)
    open_trades = await db.get_open_trades(user_id)
    for t in open_trades:
        if t["symbol"] == signal.symbol:
            logger.info("User %s already has open trade on %s — skipped",
                        user_id, signal.symbol)
            return

    if len(open_trades) >= MAX_OPEN_TRADES:
        await _notify(app, user_id,
            f"⚠️ Max open trades ({MAX_OPEN_TRADES}) reached. "
            f"`{signal.symbol}` signal skipped.")
        return

    leverage   = user.get("leverage") or DEFAULT_LEVERAGE
    margin_tp  = user.get("margin_type") or DEFAULT_MARGIN_TYPE
    pos_size   = user.get("position_size") or DEFAULT_POSITION_SIZE

    try:
        engine    = BinanceFutures(api_key, api_secret, testnet=BINANCE_TESTNET)
        balance   = engine.get_balance()
        available = float(balance.get("withdrawAvailable", 0))
        usdt_amt  = available * (pos_size / 100)

        if usdt_amt < 5:
            await _notify(app, user_id,
                f"⚠️ Insufficient balance (`${available:.2f}` USDT). "
                f"Signal skipped for `{signal.symbol}`.")
            return

        qty = engine.calc_qty(signal.symbol, usdt_amt, leverage)
        if qty <= 0:
            await _notify(app, user_id,
                f"⚠️ Could not calculate valid quantity for `{signal.symbol}`.")
            return

        # Use safe_sl for the actual SL order (more conservative)
        result = engine.open_market(
            symbol      = signal.symbol,
            side        = signal.side,
            qty         = qty,
            leverage    = leverage,
            margin_type = margin_tp,
            sl_price    = signal.safe_sl,
            tp_price    = signal.tp,
        )

        order      = result["order"]
        sl_ord_id  = result["sl_order_id"]
        tp_ord_id  = result["tp_order_id"]
        entry_price = float(order.get("avgPrice") or signal.entry)

        # Place DCA limit order
        dca_order_id = ""
        dca_qty      = engine._round_qty(signal.symbol, qty * 0.5)  # 50% of position
        if dca_qty > 0:
            dca_order_id = engine.place_dca_limit(
                signal.symbol, signal.side, dca_qty, signal.dca)

        # Save trade to DB (initial TSL = safe_sl)
        trade_id = await db.open_trade(
            user_id          = user_id,
            symbol           = signal.symbol,
            side             = signal.side,
            qty              = qty,
            entry_price      = entry_price,
            sl               = signal.sl,
            safe_sl          = signal.safe_sl,
            tp               = signal.tp,
            dca_price        = signal.dca,
            dca_order_id     = dca_order_id,
            current_tsl      = signal.safe_sl,   # starts at safe SL
            leverage         = leverage,
            margin_type      = margin_tp,
            binance_order_id = str(order.get("orderId", "")),
            sl_order_id      = sl_ord_id,
            tp_order_id      = tp_ord_id,
            status           = "open",
            signal_id        = signal_id,
        )

        side_emoji = "🟢 LONG" if signal.side == "BUY" else "🔴 SHORT"
        msg = (
            f"✅ *Trade Opened*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {signal.symbol}  {side_emoji}\n"
            f"💵 Entry   : `{entry_price}`\n"
            f"🛑 SL      : `{signal.safe_sl}`\n"
            f"🎯 TP      : `{signal.tp}`\n"
            f"🔄 DCA     : `{signal.dca}`\n"
            f"⚖️ R:R     : `1:{signal.rr}`\n"
            f"⚡ Leverage: `{leverage}x`\n"
            f"💰 USDT    : `${usdt_amt:.2f}`\n"
            f"📦 Qty     : `{qty}`\n"
            f"📊 MTF     : `{signal.mtf_score}/5`\n"
            f"🆔 Trade ID: `{trade_id}`\n\n"
            f"🔒 Trailing SL is active — updates automatically."
        )
        await _notify(app, user_id, msg)

    except Exception as e:
        logger.error("Execute error user=%s symbol=%s: %s", user_id, signal.symbol, e)
        await _notify(app, user_id,
            f"❌ Failed to open trade for `{signal.symbol}`:\n`{str(e)[:200]}`")


async def run_scan(app: Application):
    """Main scanner — runs all symbols through TradeX strategy."""
    logger.info("▶ Scan started @ %s UTC", datetime.utcnow().strftime("%H:%M:%S"))

    try:
        mkt_client = Client(
            BINANCE_API_KEY or "", BINANCE_API_SECRET or "",
            testnet=BINANCE_TESTNET
        )
    except Exception as e:
        logger.error("Binance client error: %s", e)
        return

    global_cfg = _global_cfg()
    engine     = TradeXEngine(mkt_client, global_cfg)
    auto_users = await db.get_auto_users()

    for symbol in SCAN_SYMBOLS:
        signal = engine.analyze(symbol, SCAN_TIMEFRAME)

        if signal is None:
            continue

        # MTF filter
        if signal.mtf_score < MTF_CONFIRM_MIN_SCORE:
            logger.info("Signal %s %s MTF=%d/5 below threshold — skipped",
                        symbol, signal.side, signal.mtf_score)
            continue

        logger.info("🔔 SIGNAL %s %s  MTF=%d/5  entry=%s  SL=%s  TP=%s",
                    symbol, signal.side, signal.mtf_score,
                    signal.entry, signal.safe_sl, signal.tp)

        # Save to signals table
        signal_id = await db.save_signal(
            symbol      = signal.symbol,
            timeframe   = signal.timeframe,
            side        = signal.side,
            entry_price = signal.entry,
            sl          = signal.sl,
            safe_sl     = signal.safe_sl,
            tp          = signal.tp,
            dca_price   = signal.dca,
            mtf_score   = signal.mtf_score,
            mtf_detail  = json.dumps(signal.mtf_detail),
        )

        # Broadcast signal card to all admins
        for admin_id in ADMIN_IDS:
            await _notify(app, admin_id, signal.to_telegram())

        # Execute trade for each auto-trade user
        for user in auto_users:
            await _execute_for_user(app, user, signal, signal_id)

        await asyncio.sleep(1.5)   # rate-limit between symbols

    logger.info("✅ Scan complete")
