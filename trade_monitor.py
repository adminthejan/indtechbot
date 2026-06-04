"""
Trade Monitor
=============
Runs on a 1-minute loop. For every open trade it:

1. Checks if SL or TP order was already filled by Binance
   → marks trade closed, calculates PnL, notifies user

2. Checks if Supertrend has flipped direction (exit signal)
   → closes position at market, cancels remaining orders

3. Updates Trailing SL (moves SL only in profit direction)
   → cancels old SL order, places new one, updates DB

All three exit reasons are logged as: TP | SL | FLIP | TSL
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
    SCAN_TIMEFRAME, ST_ATR_PERIOD, ST_MULTIPLIER,
    SL_BUFFER_ATR, TSL_BUFFER_ATR, TP_RR_RATIO, TP_MODE,
    TP_PERCENT, DCA_METHOD, MTF_PIVOT_LEN,
)
from database import db
from strategy import TradeXEngine
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
    if user.get("st_multiplier") is not None:
        cfg["st_multiplier"]  = user["st_multiplier"]
    if user.get("sl_buffer") is not None:
        cfg["sl_buffer_atr"]  = user["sl_buffer"]
    if user.get("tp_rr") is not None:
        cfg["tp_rr"]          = user["tp_rr"]
    if user.get("tp_mode") is not None:
        cfg["tp_mode"]        = user["tp_mode"]
    if user.get("dca_method") is not None:
        cfg["dca_method"]     = user["dca_method"]
    return cfg


async def _notify(app: Application, user_id: int, text: str):
    try:
        await app.bot.send_message(user_id, text, parse_mode="Markdown")
    except Exception as e:
        logger.warning("Notify %s: %s", user_id, e)


def _pnl(trade: dict, exit_price: float) -> tuple[float, float]:
    """Return (pnl_usdt, pnl_pct)."""
    entry = trade["entry_price"] or 0
    qty   = trade["qty"]         or 0
    lev   = trade["leverage"]    or 1
    if trade["side"] == "BUY":
        raw_pct = (exit_price - entry) / entry if entry else 0
    else:
        raw_pct = (entry - exit_price) / entry if entry else 0
    pnl_pct  = raw_pct * lev * 100
    pnl_usdt = qty * (exit_price - entry) * (1 if trade["side"] == "BUY" else -1)
    return round(pnl_usdt, 4), round(pnl_pct, 2)


async def _close_trade_in_db(trade: dict, exit_price: float,
                              reason: str, app: Application):
    pnl, pnl_pct = _pnl(trade, exit_price)
    await db.close_trade(trade["id"], exit_price, reason, pnl, pnl_pct)
    icon = "✅" if pnl >= 0 else "❌"
    pnl_icon = "🟢" if pnl >= 0 else "🔴"
    side_icon = "🟢 LONG" if trade["side"] == "BUY" else "🔴 SHORT"

    reason_label = {
        "TP":     "🎯 Take Profit Hit",
        "SL":     "🛑 Stop Loss Hit",
        "TSL":    "🔒 Trailing SL Hit",
        "FLIP":   "🔄 Supertrend Flipped — Exit",
        "MANUAL": "🖐 Manual Close",
    }.get(reason, reason)

    await _notify(app, trade["user_id"],
        f"{icon} *Trade Closed*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 {trade['symbol']}  {side_icon}\n"
        f"📌 Reason  : {reason_label}\n"
        f"💵 Entry   : `{trade['entry_price']}`\n"
        f"💵 Exit    : `{exit_price}`\n"
        f"{pnl_icon} PnL    : `${pnl:+.4f}` (`{pnl_pct:+.2f}%`)\n"
        f"🆔 Trade ID: `{trade['id']}`"
    )


async def monitor_trade(trade: dict, app: Application):
    """Check and manage a single open trade."""
    user = await db.get_user(trade["user_id"])
    if not user:
        return

    api_key    = user.get("binance_key", "")
    api_secret = user.get("binance_secret", "")
    if not api_key or not api_secret:
        return

    symbol    = trade["symbol"]
    side      = trade["side"]
    timeframe = SCAN_TIMEFRAME

    try:
        engine  = BinanceFutures(api_key, api_secret, testnet=BINANCE_TESTNET)
        price   = engine.get_price(symbol)
    except Exception as e:
        logger.warning("Monitor price error %s: %s", symbol, e)
        return

    # ── 1. Check if TP order was filled by Binance ────────────────────────────
    tp_id = trade.get("tp_order_id", "")
    if tp_id and not engine.is_order_active(symbol, tp_id):
        # TP likely hit — check position size
        pos_size = engine.position_size(symbol)
        if pos_size == 0:
            exit_price = engine.get_fill_price(symbol, tp_id) or price
            engine.cancel_all_orders(symbol)
            await _close_trade_in_db(trade, exit_price, "TP", app)
            return

    # ── 2. Check if SL order was filled by Binance ────────────────────────────
    sl_id = trade.get("sl_order_id", "")
    if sl_id and not engine.is_order_active(symbol, sl_id):
        pos_size = engine.position_size(symbol)
        if pos_size == 0:
            exit_price = engine.get_fill_price(symbol, sl_id) or price
            engine.cancel_all_orders(symbol)
            reason = "TSL" if trade.get("current_tsl") else "SL"
            await _close_trade_in_db(trade, exit_price, reason, app)
            return

    # ── 2.5. Check if DCA limit order was filled ──────────────────────────────
    # When DCA fills, price has pulled back to the DCA zone.
    # Move SL to safe_sl (SMC structural level) to give the averaged
    # position more breathing room.
    current_tsl = trade.get("current_tsl")
    dca_id      = trade.get("dca_order_id", "")
    if dca_id and not engine.is_order_active(symbol, dca_id):
        dca_fill_price = engine.get_fill_price(symbol, dca_id)
        if dca_fill_price:
            # DCA confirmed filled — widen SL to safe_sl
            safe_sl_val = trade.get("safe_sl")
            if safe_sl_val:
                new_sl_id   = engine.update_sl(symbol, side, sl_id, safe_sl_val)
                sl_id       = new_sl_id          # keep in sync for TSL step below
                current_tsl = safe_sl_val
                await db.update_trade(trade["id"],
                                      sl_order_id  = new_sl_id,
                                      current_tsl  = safe_sl_val,
                                      dca_order_id = "")
                logger.info("DCA filled %s @ %s — SL moved to safe_sl %s",
                            symbol, dca_fill_price, safe_sl_val)
                await _notify(app, trade["user_id"],
                    f"📥 *DCA Filled*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🪙 {symbol} | {'🟢 LONG' if side == 'BUY' else '🔴 SHORT'}\n"
                    f"💵 DCA Fill  : `{dca_fill_price}`\n"
                    f"🛡️ SL → Safe SL : `{safe_sl_val}`\n"
                    f"💡 Position averaged — SL widened to structural level."
                )
        else:
            # Order inactive but not filled — was cancelled, clear it
            await db.update_trade(trade["id"], dca_order_id="")

    # ── 3. Check Supertrend direction flip (dynamic exit) ─────────────────────
    mkt_client = Client(BINANCE_API_KEY or "", BINANCE_API_SECRET or "",
                        testnet=BINANCE_TESTNET)
    cfg        = _user_cfg(user)
    st_engine  = TradeXEngine(mkt_client, cfg)
    cur_dir    = st_engine.supertrend_direction(symbol, timeframe)

    if cur_dir is not None:
        # BUY trade should exit when ST turns bearish (dir=1)
        # SELL trade should exit when ST turns bullish (dir=-1)
        should_flip_exit = (side == "BUY" and cur_dir == 1) or \
                           (side == "SELL" and cur_dir == -1)

        if should_flip_exit:
            logger.info("FLIP EXIT %s %s", symbol, side)
            try:
                pos_size = engine.position_size(symbol)
                if pos_size > 0:
                    engine.close_market(symbol, side, pos_size)
                engine.cancel_all_orders(symbol)
                await _close_trade_in_db(trade, price, "FLIP", app)
                return
            except Exception as e:
                logger.error("Flip exit error %s: %s", symbol, e)

    # ── 4. Update Trailing SL (profit-scaled buffer) ──────────────────────────
    entry_price  = trade.get("entry_price") or 0
    initial_risk = abs(entry_price - (trade.get("sl") or entry_price))

    new_tsl = st_engine.calc_trailing_sl(
        symbol, timeframe, side,
        current_tsl  = current_tsl,
        entry_price  = entry_price  if entry_price  else None,
        initial_risk = initial_risk if initial_risk > 0 else None,
    )
    if new_tsl is None:
        return

    # Only update if TSL has moved in favour by at least 0.01%
    tsl_changed = (
        current_tsl is None or
        (side == "BUY"  and new_tsl > current_tsl * 1.0001) or
        (side == "SELL" and new_tsl < current_tsl * 0.9999)
    )

    if tsl_changed:
        new_sl_id = engine.update_sl(symbol, side, sl_id, new_tsl)
        await db.update_trade(trade["id"],
                              current_tsl  = new_tsl,
                              sl_order_id  = new_sl_id)
        logger.info("TSL updated %s %s → %s", symbol, side, new_tsl)

        # Notify user of TSL move (only if it moved significantly, >0.5%)
        prev     = current_tsl or trade.get("safe_sl", 0)
        move_pct = abs(new_tsl - prev) / prev * 100 if prev else 0
        if move_pct > 0.5:
            pnl_pct = (
                (price - entry_price) / entry_price * trade.get("leverage", 1) * 100
                if side == "BUY"
                else (entry_price - price) / entry_price * trade.get("leverage", 1) * 100
            )
            await _notify(app, trade["user_id"],
                f"🔒 *Trailing SL Updated*\n"
                f"🪙 {symbol} | {'🟢 LONG' if side == 'BUY' else '🔴 SHORT'}\n"
                f"🛑 New TSL : `{new_tsl}`\n"
                f"💵 Price   : `{price}`\n"
                f"📈 PnL     : `{pnl_pct:+.2f}%`"
            )


async def run_monitor(app: Application):
    """Monitor all open trades — called every MONITOR_INTERVAL_MINUTES."""
    open_trades = await db.get_open_trades()
    if not open_trades:
        return
    logger.info("Monitor: checking %d open trade(s)", len(open_trades))
    tasks = [monitor_trade(t, app) for t in open_trades]
    await asyncio.gather(*tasks, return_exceptions=True)
