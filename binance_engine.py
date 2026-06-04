"""
Binance Futures (USDT-M) execution layer.
Handles: open, close, SL/TP orders, trailing SL updates, DCA limit orders.
"""

from __future__ import annotations
import logging
from typing import Optional
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

logger = logging.getLogger(__name__)

SIDE_BUY_STR  = "BUY"
SIDE_SELL_STR = "SELL"


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.client = Client(api_key, api_secret, testnet=testnet)
        if testnet:
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

    # ── Account info ──────────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        balances = self.client.futures_account_balance()
        usdt = next((b for b in balances if b["asset"] == "USDT"), None)
        return usdt or {"balance": "0", "withdrawAvailable": "0"}

    def get_positions(self) -> list[dict]:
        return [p for p in self.client.futures_position_information()
                if float(p["positionAmt"]) != 0]

    def get_open_orders(self, symbol: str) -> list[dict]:
        return self.client.futures_get_open_orders(symbol=symbol)

    def get_price(self, symbol: str) -> float:
        return float(self.client.futures_symbol_ticker(symbol=symbol)["price"])

    # ── Symbol info ───────────────────────────────────────────────────────────

    def _get_filters(self, symbol: str) -> dict:
        info = self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return {f["filterType"]: f for f in s["filters"]}
        return {}

    def _round_price(self, symbol: str, price: float) -> float:
        filters = self._get_filters(symbol)
        tick = float(filters.get("PRICE_FILTER", {}).get("tickSize", "0.01"))
        if tick == 0:
            return round(price, 4)
        precision = len(str(tick).rstrip("0").split(".")[-1]) if "." in str(tick) else 0
        return round(round(price / tick) * tick, precision)

    def _round_qty(self, symbol: str, qty: float) -> float:
        filters = self._get_filters(symbol)
        step = float(filters.get("LOT_SIZE", {}).get("stepSize", "0.001"))
        if step == 0:
            return qty
        precision = len(str(step).rstrip("0").split(".")[-1]) if "." in str(step) else 0
        return round(qty - (qty % step), precision)

    def calc_qty(self, symbol: str, usdt_amount: float, leverage: int) -> float:
        price    = self.get_price(symbol)
        notional = usdt_amount * leverage
        qty      = notional / price
        return self._round_qty(symbol, qty)

    # ── Order setup ───────────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except BinanceAPIException as e:
            logger.warning("Leverage %s: %s", symbol, e)

    def set_margin_type(self, symbol: str, margin_type: str):
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
        except BinanceAPIException as e:
            if e.code != -4046:   # -4046 = already set, ignore
                logger.warning("MarginType %s: %s", symbol, e)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def open_market(
        self,
        symbol:      str,
        side:        str,         # BUY | SELL
        qty:         float,
        leverage:    int   = 10,
        margin_type: str   = "ISOLATED",
        sl_price:    float = None,
        tp_price:    float = None,
    ) -> dict:
        """
        Open a futures market order + SL stop-market + TP take-profit-market.
        Returns dict with keys: order, sl_order_id, tp_order_id
        """
        self.set_margin_type(symbol, margin_type)
        self.set_leverage(symbol, leverage)

        order = self.client.futures_create_order(
            symbol=symbol,
            side=SIDE_BUY if side == SIDE_BUY_STR else SIDE_SELL,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
        )
        logger.info("ENTRY %s %s qty=%s order_id=%s", side, symbol, qty, order["orderId"])

        close_side = SIDE_SELL if side == SIDE_BUY_STR else SIDE_BUY
        sl_id = tp_id = ""

        if sl_price:
            sl_price = self._round_price(symbol, sl_price)
            try:
                sl_ord = self.client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type=FUTURE_ORDER_TYPE_STOP_MARKET,
                    stopPrice=sl_price,
                    closePosition=True,
                    timeInForce="GTC",
                )
                sl_id = str(sl_ord["orderId"])
                logger.info("SL order placed @ %s id=%s", sl_price, sl_id)
            except BinanceAPIException as e:
                logger.error("SL order failed: %s", e)

        if tp_price:
            tp_price = self._round_price(symbol, tp_price)
            try:
                tp_ord = self.client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                    stopPrice=tp_price,
                    closePosition=True,
                    timeInForce="GTC",
                )
                tp_id = str(tp_ord["orderId"])
                logger.info("TP order placed @ %s id=%s", tp_price, tp_id)
            except BinanceAPIException as e:
                logger.error("TP order failed: %s", e)

        return {
            "order":       order,
            "sl_order_id": sl_id,
            "tp_order_id": tp_id,
        }

    def place_dca_limit(self, symbol: str, side: str,
                        qty: float, price: float) -> str:
        """
        Place a DCA limit order at dca_price.
        Returns order id or empty string on failure.
        """
        price = self._round_price(symbol, price)
        qty   = self._round_qty(symbol, qty)
        try:
            ord_ = self.client.futures_create_order(
                symbol=symbol,
                side=SIDE_BUY if side == SIDE_BUY_STR else SIDE_SELL,
                type=ORDER_TYPE_LIMIT,
                price=price,
                quantity=qty,
                timeInForce=TIME_IN_FORCE_GTC,
            )
            logger.info("DCA limit placed @ %s id=%s", price, ord_["orderId"])
            return str(ord_["orderId"])
        except BinanceAPIException as e:
            logger.error("DCA limit failed: %s", e)
            return ""

    # ── Exit ──────────────────────────────────────────────────────────────────

    def close_market(self, symbol: str, side: str, qty: float) -> dict:
        """Close an open position with a market order (reduceOnly)."""
        close_side = SIDE_SELL if side == SIDE_BUY_STR else SIDE_BUY
        order = self.client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type=FUTURE_ORDER_TYPE_MARKET,
            quantity=qty,
            reduceOnly=True,
        )
        logger.info("EXIT %s qty=%s", symbol, qty)
        return order

    def cancel_order(self, symbol: str, order_id: str):
        if not order_id:
            return
        try:
            self.client.futures_cancel_order(symbol=symbol, orderId=int(order_id))
        except BinanceAPIException as e:
            if e.code != -2011:   # -2011 = unknown order, already filled/cancelled
                logger.warning("Cancel order %s: %s", order_id, e)

    def cancel_all_orders(self, symbol: str):
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
        except BinanceAPIException as e:
            logger.warning("Cancel all orders %s: %s", symbol, e)

    # ── Update trailing SL ────────────────────────────────────────────────────

    def update_sl(self, symbol: str, side: str,
                  old_sl_id: str, new_sl_price: float) -> str:
        """
        Cancel existing SL order and place a new one at new_sl_price.
        Returns new SL order id.
        """
        self.cancel_order(symbol, old_sl_id)
        new_sl_price = self._round_price(symbol, new_sl_price)
        close_side   = SIDE_SELL if side == SIDE_BUY_STR else SIDE_BUY
        try:
            ord_ = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice=new_sl_price,
                closePosition=True,
                timeInForce="GTC",
            )
            logger.info("TSL updated @ %s id=%s", new_sl_price, ord_["orderId"])
            return str(ord_["orderId"])
        except BinanceAPIException as e:
            logger.error("TSL update failed: %s", e)
            return old_sl_id

    # ── Check if order is still active ───────────────────────────────────────

    def is_order_active(self, symbol: str, order_id: str) -> bool:
        if not order_id:
            return False
        try:
            ord_ = self.client.futures_get_order(symbol=symbol, orderId=int(order_id))
            return ord_["status"] in ("NEW", "PARTIALLY_FILLED")
        except BinanceAPIException:
            return False

    def get_fill_price(self, symbol: str, order_id: str) -> Optional[float]:
        """Return avg fill price of a completed order."""
        try:
            ord_ = self.client.futures_get_order(symbol=symbol, orderId=int(order_id))
            avg  = float(ord_.get("avgPrice") or 0)
            return avg if avg > 0 else None
        except BinanceAPIException:
            return None

    def position_size(self, symbol: str) -> float:
        """Return absolute current position size (0 if no position)."""
        for p in self.client.futures_position_information(symbol=symbol):
            amt = float(p["positionAmt"])
            if amt != 0:
                return abs(amt)
        return 0.0
