"""
IndTech TradeX Pro — Full Python Strategy Engine
=================================================
Exact port of the PineScript indicator logic:

  Signal generation  : Supertrend direction flip (confirmed bar)
  Stop Loss          : low/high ± ATR × sl_buffer  (wick protection)
  Safe SL            : last SMC pivot low/high
  Take Profit        : Risk × RR  |  entry ± %
  DCA price          : FVG midpoint | Fib 61.8% | 50% midpoint
  Trailing SL        : SMC swing point | Supertrend line  + ATR buffer
  MTF confirmation   : 5m / 15m / 1h / 4h / 1d
                       score = Supertrend + EMA200 + RSI (mirrors Pine)
"""

from __future__ import annotations
import logging
import math
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional
from binance.client import Client

logger = logging.getLogger(__name__)

# ── Binance interval map ───────────────────────────────────────────────────────
TF_MAP = {
    "1m":  "1m",  "3m":  "3m",  "5m":  "5m",
    "15m": "15m", "30m": "30m", "1h":  "1h",
    "2h":  "2h",  "4h":  "4h",  "6h":  "6h",
    "12h": "12h", "1d":  "1d",  "1w":  "1w",
}
MTF_ORDER = ["5m", "15m", "1h", "4h", "1d"]


# ── Signal dataclass ───────────────────────────────────────────────────────────

@dataclass
class Signal:
    symbol:      str
    timeframe:   str
    side:        str        # BUY | SELL
    entry:       float
    sl:          float      # ATR-buffered SL
    safe_sl:     float      # SMC swing-point SL (safer, used for actual order)
    tp:          float
    dca:         float
    atr:         float
    mtf_score:   int        # 0–5
    mtf_detail:  dict       # {"5m": 1, "15m": -1, ...}

    @property
    def risk(self) -> float:
        return abs(self.entry - self.safe_sl)

    @property
    def rr(self) -> float:
        reward = abs(self.tp - self.entry)
        return round(reward / self.risk, 2) if self.risk > 0 else 0.0

    def to_telegram(self) -> str:
        hdr  = "🐂 BUY SETUP 🟢" if self.side == "BUY" else "🐻 SELL SETUP 🔴"
        icons = {1: "📈 BULLISH", -1: "📉 BEARISH", 0: "⚖️ NEUTRAL"}
        mtf  = "\n".join(
            f"  `{tf:>4s}` : {icons.get(v, '?')}"
            for tf, v in self.mtf_detail.items()
        )
        return (
            f"🚨 *{hdr}* 🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Symbol    : `{self.symbol}`\n"
            f"🕒 Timeframe : `{self.timeframe}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Entry     : `{self.entry}`\n"
            f"🔄 DCA       : `{self.dca}`\n"
            f"🛑 SL        : `{self.sl}`\n"
            f"🛡️ Safe SL   : `{self.safe_sl}`\n"
            f"🎯 TP        : `{self.tp}`\n"
            f"⚖️ R:R       : `1:{self.rr}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 *MTF Confirmation* `{self.mtf_score}/5`\n"
            f"{mtf}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Margin    : 1%–5% MAX\n"
            f"⚡ Leverage  : 10x–25x (YOUR RISK)\n"
            f"⚠️ Always use SL. High risk!\n"
            f"📊 *IndTech TradeX Pro*"
        )


# ── Indicator helpers ──────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def _supertrend(df: pd.DataFrame, mult: float, period: int):
    """
    Returns (st_line, direction).
    direction == -1 → bullish (price above line)
    direction ==  1 → bearish (price below line)
    """
    atr  = _atr(df, period)
    hl2  = (df["high"] + df["low"]) / 2
    ub   = hl2 + mult * atr
    lb   = hl2 - mult * atr

    st  = pd.Series(np.nan, index=df.index)
    dir_ = pd.Series(0,    index=df.index, dtype=int)

    for i in range(1, len(df)):
        prev_st  = st.iloc[i - 1]
        prev_dir = dir_.iloc[i - 1]
        c        = df["close"].iloc[i]
        pc       = df["close"].iloc[i - 1]

        # Carry-forward bands (Pine style)
        cur_ub = ub.iloc[i]
        cur_lb = lb.iloc[i]
        if not np.isnan(prev_st):
            if prev_dir == 1:    # was bearish
                cur_ub = min(cur_ub, prev_st)
            else:                # was bullish
                cur_lb = max(cur_lb, prev_st)

        if prev_dir <= 0:        # was bullish or start
            if c < cur_lb:
                dir_.iloc[i] = 1
                st.iloc[i]   = cur_ub
            else:
                dir_.iloc[i] = -1
                st.iloc[i]   = cur_lb
        else:                    # was bearish
            if c > cur_ub:
                dir_.iloc[i] = -1
                st.iloc[i]   = cur_lb
            else:
                dir_.iloc[i] = 1
                st.iloc[i]   = cur_ub

    return st, dir_


def _pivot_high(df: pd.DataFrame, period: int) -> pd.Series:
    ph = pd.Series(np.nan, index=df.index)
    h  = df["high"].values
    for i in range(period, len(df) - period):
        win = h[i - period: i + period + 1]
        if h[i] == win.max():
            ph.iloc[i] = h[i]
    return ph


def _pivot_low(df: pd.DataFrame, period: int) -> pd.Series:
    pl = pd.Series(np.nan, index=df.index)
    l  = df["low"].values
    for i in range(period, len(df) - period):
        win = l[i - period: i + period + 1]
        if l[i] == win.min():
            pl.iloc[i] = l[i]
    return pl


def _fvg(df: pd.DataFrame):
    """Last bullish and bearish Fair Value Gap midpoints."""
    bull = bear = None
    for i in range(2, len(df)):
        if df["low"].iloc[i] > df["high"].iloc[i - 2]:          # bullish FVG
            bull = (df["low"].iloc[i] + df["high"].iloc[i - 2]) / 2
        if df["high"].iloc[i] < df["low"].iloc[i - 2]:          # bearish FVG
            bear = (df["low"].iloc[i - 2] + df["high"].iloc[i]) / 2
    return bull, bear


def _ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["close"].ewm(span=period, adjust=False).mean()


def _rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    d  = df["close"].diff()
    g  = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    ls = (-d).clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    rs = g / ls.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _trend_score(df: pd.DataFrame, mult: float, period: int) -> int:
    """
    Mirrors Pine get_trend_score():
      bullish = dir==-1 AND close > EMA200 AND RSI > 50  → +1
      bearish = dir==1  AND close < EMA200 AND RSI < 50  → -1
      else                                                → 0
    """
    if len(df) < 210:
        return 0
    _, dir_ = _supertrend(df, mult, period)
    ema200  = _ema(df, 200)
    rsi     = _rsi(df, 14)
    d = dir_.iloc[-1]
    c = df["close"].iloc[-1]
    e = ema200.iloc[-1]
    r = rsi.iloc[-1]
    if d == -1 and c > e and r > 50:
        return 1
    if d == 1 and c < e and r < 50:
        return -1
    return 0


# ── Kline fetcher ──────────────────────────────────────────────────────────────

def fetch_klines(client: Client, symbol: str, interval: str,
                 limit: int = 310) -> pd.DataFrame:
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df  = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qv","trades","tbb","tbq","ignore"
    ])
    for col in ("open","high","low","close","volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    # Drop the last (currently open) candle — only use confirmed bars
    return df.iloc[:-1]


# ── Main engine ────────────────────────────────────────────────────────────────

class TradeXEngine:
    def __init__(self, client: Client, cfg: dict):
        self.client = client
        self.cfg    = cfg

    # ── Per-symbol analysis ────────────────────────────────────────────────────

    def analyze(self, symbol: str, timeframe: str) -> Optional[Signal]:
        """
        Run full TradeX analysis on the last confirmed bar.
        Returns Signal on BUY/SELL flip, None otherwise.
        """
        try:
            df = fetch_klines(self.client, symbol, timeframe)
        except Exception as e:
            logger.error("Kline error %s %s: %s", symbol, timeframe, e)
            return None

        if len(df) < 50:
            return None

        mult   = self.cfg["st_multiplier"]
        period = self.cfg["st_atr_period"]
        pivot  = self.cfg["mtf_pivot_len"]

        st_line, direction = _supertrend(df, mult, period)

        # Signal on last confirmed bar (index -1 after dropping open candle)
        cur_dir  = direction.iloc[-1]
        prev_dir = direction.iloc[-2]

        buy  = (cur_dir == -1 and prev_dir == 1)
        sell = (cur_dir == 1  and prev_dir == -1)
        if not buy and not sell:
            return None

        side    = "BUY" if buy else "SELL"
        entry   = df["close"].iloc[-1]
        hi      = df["high"].iloc[-1]
        lo      = df["low"].iloc[-1]
        atr14   = _atr(df, 14).iloc[-1]
        sl_buf  = self.cfg["sl_buffer_atr"]

        # ── SL (ATR wick protection) ──────────────────────────────────────────
        sl = (lo - atr14 * sl_buf) if side == "BUY" else (hi + atr14 * sl_buf)

        # ── Safe SL (SMC swing point) ─────────────────────────────────────────
        ph_s = _pivot_high(df, pivot).dropna()
        pl_s = _pivot_low(df,  pivot).dropna()
        last_ph = ph_s.iloc[-1] if not ph_s.empty else sl
        last_pl = pl_s.iloc[-1] if not pl_s.empty else sl
        safe_sl = last_pl if side == "BUY" else last_ph

        # Fallback: if pivot is beyond current SL, use ATR SL
        if side == "BUY" and safe_sl > entry:
            safe_sl = sl
        if side == "SELL" and safe_sl < entry:
            safe_sl = sl

        # ── Take Profit ───────────────────────────────────────────────────────
        # risk uses ATR SL (matches Pine: abs(entry - stop_loss))
        risk = abs(entry - sl)
        if self.cfg["tp_mode"] == "RR":
            tp = (entry + risk * self.cfg["tp_rr"]) if side == "BUY" \
                 else (entry - risk * self.cfg["tp_rr"])
        else:
            pct = self.cfg["tp_percent"] / 100
            tp  = entry * (1 + pct) if side == "BUY" else entry * (1 - pct)

        # ── Smart DCA ─────────────────────────────────────────────────────────
        # dca_risk uses ATR SL distance (matches Pine: abs(entry - stop_loss))
        bull_fvg, bear_fvg = _fvg(df)
        method = self.cfg["dca_method"]

        if side == "BUY":
            dca = entry - risk * 0.618          # Fib 61.8% default
            if method == "FVG":
                if bull_fvg and sl < bull_fvg < entry:
                    dca = bull_fvg              # FVG fill zone
                elif not pl_s.empty and sl < last_pl < entry:
                    dca = last_pl               # liquidity grab
            elif method == "FIB618":
                dca = entry - risk * 0.618
            elif method == "MID50":
                dca = entry - risk * 0.5
        else:
            dca = entry + risk * 0.618
            if method == "FVG":
                if bear_fvg and entry < bear_fvg < sl:
                    dca = bear_fvg
                elif not ph_s.empty and entry < last_ph < sl:
                    dca = last_ph
            elif method == "FIB618":
                dca = entry + risk * 0.618
            elif method == "MID50":
                dca = entry + risk * 0.5

        # ── MTF confirmation ──────────────────────────────────────────────────
        mtf_detail: dict[str, int] = {}
        for tf in MTF_ORDER:
            try:
                df_tf = fetch_klines(self.client, symbol, tf, limit=250)
                mtf_detail[tf] = _trend_score(df_tf, mult, period)
            except Exception as e:
                logger.warning("MTF %s %s: %s", symbol, tf, e)
                mtf_detail[tf] = 0

        target = 1 if side == "BUY" else -1
        mtf_score = sum(1 for v in mtf_detail.values() if v == target)

        def r(x): return round(x, 6)

        return Signal(
            symbol=symbol, timeframe=timeframe, side=side,
            entry=r(entry), sl=r(sl), safe_sl=r(safe_sl),
            tp=r(tp), dca=r(dca), atr=r(atr14),
            mtf_score=mtf_score, mtf_detail=mtf_detail,
        )

    # ── Trailing SL calculation ────────────────────────────────────────────────

    def calc_trailing_sl(self, symbol: str, timeframe: str,
                         side: str, current_tsl: float = None,
                         entry_price: float = None,
                         initial_risk: float = None) -> Optional[float]:
        """
        Calculate updated trailing SL price.
        Uses SMC swing point + ATR buffer (mirrors Pine TSL logic).
        TSL only moves in favour — never against the trade.
        Buffer tightens progressively as profit grows in R multiples:
          0–1R → 1.0× buffer (normal)
          1–2R → 0.7× (tighter)
          2–3R → 0.5× (tighter)
          3R+  → 0.3× (very tight — locks most profit)
        """
        try:
            df = fetch_klines(self.client, symbol, timeframe)
        except Exception:
            return None

        atr14   = _atr(df, 14).iloc[-1]
        tsl_buf = self.cfg["tsl_buffer_atr"]
        pivot   = self.cfg["mtf_pivot_len"]
        mult    = self.cfg["st_multiplier"]
        period  = self.cfg["st_atr_period"]

        st_line, _ = _supertrend(df, mult, period)
        st_now     = st_line.iloc[-1]

        ph_s = _pivot_high(df, pivot).dropna()
        pl_s = _pivot_low(df,  pivot).dropna()

        # Tighten TSL buffer as profit grows (measured in R multiples)
        if entry_price is not None and initial_risk and initial_risk > 0:
            cur_price = df["close"].iloc[-1]
            profit_r  = (
                (cur_price - entry_price) / initial_risk if side == "BUY"
                else (entry_price - cur_price) / initial_risk
            )
            if profit_r >= 3:
                tsl_buf *= 0.3
            elif profit_r >= 2:
                tsl_buf *= 0.5
            elif profit_r >= 1:
                tsl_buf *= 0.7
            logger.debug("TSL buffer scaled: profit=%.2fR buf=%.3f", profit_r, tsl_buf)

        if side == "BUY":
            # TSL below last swing low
            base = pl_s.iloc[-1] if not pl_s.empty else st_now
            new_tsl = base - atr14 * tsl_buf
            # Only move TSL up (lock in profits)
            if current_tsl is not None:
                new_tsl = max(new_tsl, current_tsl)
        else:
            # TSL above last swing high
            base = ph_s.iloc[-1] if not ph_s.empty else st_now
            new_tsl = base + atr14 * tsl_buf
            # Only move TSL down (lock in profits)
            if current_tsl is not None:
                new_tsl = min(new_tsl, current_tsl)

        return round(new_tsl, 6)

    def supertrend_direction(self, symbol: str, timeframe: str) -> Optional[int]:
        """Return current Supertrend direction for a symbol (-1=bull, 1=bear)."""
        try:
            df = fetch_klines(self.client, symbol, timeframe)
            _, direction = _supertrend(df, self.cfg["st_multiplier"],
                                       self.cfg["st_atr_period"])
            return int(direction.iloc[-1])
        except Exception:
            return None

    def current_price(self, symbol: str) -> Optional[float]:
        try:
            return float(self.client.get_symbol_ticker(symbol=symbol)["price"])
        except Exception:
            return None
