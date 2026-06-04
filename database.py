"""
Async SQLite database.
Tracks users, trades (with full entry/exit/TSL), and signals.
"""

import aiosqlite
import json
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = "tradex.db"


class Database:
    def __init__(self):
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self._db = await aiosqlite.connect(DB_PATH)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id             INTEGER PRIMARY KEY,
                username            TEXT    DEFAULT '',
                full_name           TEXT    DEFAULT '',
                role                TEXT    DEFAULT 'pending',
                binance_key         TEXT    DEFAULT '',
                binance_secret      TEXT    DEFAULT '',
                leverage            INTEGER DEFAULT 10,
                margin_type         TEXT    DEFAULT 'ISOLATED',
                position_size       REAL    DEFAULT 5.0,
                auto_trade          INTEGER DEFAULT 0,
                subscription_expiry TEXT    DEFAULT NULL,
                joined_at           TEXT    DEFAULT (datetime('now')),
                -- Per-user strategy overrides (NULL = use global)
                st_multiplier       REAL    DEFAULT NULL,
                sl_buffer           REAL    DEFAULT NULL,
                tp_rr               REAL    DEFAULT NULL,
                tp_mode             TEXT    DEFAULT NULL,
                dca_method          TEXT    DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL,
                symbol              TEXT    NOT NULL,
                side                TEXT    NOT NULL,   -- BUY | SELL
                qty                 REAL    NOT NULL,
                entry_price         REAL,
                sl                  REAL,               -- ATR stop loss
                safe_sl             REAL,               -- SMC safe stop loss
                tp                  REAL,               -- take profit
                dca_price           REAL,               -- DCA level (informational)
                dca_order_id        TEXT    DEFAULT '',  -- Binance DCA limit order id
                current_tsl         REAL    DEFAULT NULL, -- current trailing SL price
                leverage            INTEGER DEFAULT 10,
                margin_type         TEXT    DEFAULT 'ISOLATED',
                binance_order_id    TEXT    DEFAULT '',
                sl_order_id         TEXT    DEFAULT '',
                tp_order_id         TEXT    DEFAULT '',
                status              TEXT    DEFAULT 'open', -- open | closed | error
                exit_price          REAL    DEFAULT NULL,
                exit_reason         TEXT    DEFAULT NULL, -- TP | SL | TSL | FLIP | MANUAL
                pnl                 REAL    DEFAULT 0,
                pnl_pct             REAL    DEFAULT 0,
                opened_at           TEXT    DEFAULT (datetime('now')),
                closed_at           TEXT    DEFAULT NULL,
                signal_id           INTEGER DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT,
                timeframe   TEXT,
                side        TEXT,
                entry_price REAL,
                sl          REAL,
                safe_sl     REAL,
                tp          REAL,
                dca_price   REAL,
                mtf_score   INTEGER,
                mtf_detail  TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """)
        await self._db.commit()

    # ── User ─────────────────────────────────────────────────────────────────

    async def ensure_admin(self, user_id: int):
        await self._db.execute(
            "INSERT OR IGNORE INTO users (user_id, role) VALUES (?,?)", (user_id, "admin"))
        await self._db.execute(
            "UPDATE users SET role='admin' WHERE user_id=?", (user_id,))
        await self._db.commit()

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

    async def upsert_user(self, user_id: int, username: str, full_name: str):
        await self._db.execute(
            """INSERT INTO users (user_id, username, full_name) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username, full_name=excluded.full_name""",
            (user_id, username or "", full_name or ""))
        await self._db.commit()

    async def set_role(self, user_id: int, role: str):
        await self._db.execute(
            "UPDATE users SET role=? WHERE user_id=?", (role, user_id))
        await self._db.commit()

    async def set_subscription(self, user_id: int, days: int):
        expiry = (datetime.utcnow() + timedelta(days=days)).isoformat()
        await self._db.execute(
            "UPDATE users SET subscription_expiry=?, role='user' WHERE user_id=?",
            (expiry, user_id))
        await self._db.commit()

    async def is_active(self, user_id: int) -> bool:
        u = await self.get_user(user_id)
        if not u:
            return False
        if u["role"] == "admin":
            return True
        if u["role"] in ("banned", "pending"):
            return False
        exp = u.get("subscription_expiry")
        if not exp:
            return False
        return datetime.fromisoformat(exp) > datetime.utcnow()

    async def update_keys(self, user_id: int, key: str, secret: str):
        await self._db.execute(
            "UPDATE users SET binance_key=?, binance_secret=? WHERE user_id=?",
            (key, secret, user_id))
        await self._db.commit()

    async def update_settings(self, user_id: int, **kw):
        allowed = {
            "leverage", "margin_type", "position_size", "auto_trade",
            "st_multiplier", "sl_buffer", "tp_rr", "tp_mode", "dca_method"
        }
        kw = {k: v for k, v in kw.items() if k in allowed}
        if not kw:
            return
        sets = ", ".join(f"{k}=?" for k in kw)
        await self._db.execute(
            f"UPDATE users SET {sets} WHERE user_id=?",
            list(kw.values()) + [user_id])
        await self._db.commit()

    async def list_users(self, role: str = None) -> list[dict]:
        q = "SELECT * FROM users WHERE role=? ORDER BY joined_at DESC" if role \
            else "SELECT * FROM users ORDER BY joined_at DESC"
        args = (role,) if role else ()
        async with self._db.execute(q, args) as c:
            return [dict(r) for r in await c.fetchall()]

    async def get_auto_users(self) -> list[dict]:
        async with self._db.execute("""
            SELECT * FROM users WHERE auto_trade=1
              AND role NOT IN ('banned','pending')
              AND (role='admin' OR
                   (subscription_expiry IS NOT NULL
                    AND subscription_expiry > datetime('now')))
        """) as c:
            return [dict(r) for r in await c.fetchall()]

    # ── Trades ────────────────────────────────────────────────────────────────

    async def open_trade(self, **kw) -> int:
        cols = ", ".join(kw.keys())
        ph   = ", ".join("?" for _ in kw)
        cur  = await self._db.execute(
            f"INSERT INTO trades ({cols}) VALUES ({ph})", list(kw.values()))
        await self._db.commit()
        return cur.lastrowid

    async def update_trade(self, trade_id: int, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        await self._db.execute(
            f"UPDATE trades SET {sets} WHERE id=?",
            list(kw.values()) + [trade_id])
        await self._db.commit()

    async def close_trade(self, trade_id: int, exit_price: float,
                          exit_reason: str, pnl: float, pnl_pct: float):
        await self._db.execute("""
            UPDATE trades SET
                status='closed', exit_price=?, exit_reason=?,
                pnl=?, pnl_pct=?, closed_at=datetime('now')
            WHERE id=?
        """, (exit_price, exit_reason, pnl, pnl_pct, trade_id))
        await self._db.commit()

    async def get_open_trades(self, user_id: int = None) -> list[dict]:
        if user_id:
            async with self._db.execute(
                "SELECT * FROM trades WHERE user_id=? AND status='open' ORDER BY opened_at",
                (user_id,)) as c:
                return [dict(r) for r in await c.fetchall()]
        async with self._db.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY opened_at") as c:
            return [dict(r) for r in await c.fetchall()]

    async def get_recent_trades(self, user_id: int, limit: int = 5) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY opened_at DESC LIMIT ?",
            (user_id, limit)) as c:
            return [dict(r) for r in await c.fetchall()]

    async def count_open(self, user_id: int) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) FROM trades WHERE user_id=? AND status='open'",
            (user_id,)) as c:
            return (await c.fetchone())[0]

    # ── Signals ───────────────────────────────────────────────────────────────

    async def save_signal(self, **kw) -> int:
        cols = ", ".join(kw.keys())
        ph   = ", ".join("?" for _ in kw)
        cur  = await self._db.execute(
            f"INSERT INTO signals ({cols}) VALUES ({ph})", list(kw.values()))
        await self._db.commit()
        return cur.lastrowid

    async def recent_signals(self, limit: int = 5) -> list[dict]:
        async with self._db.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)) as c:
            return [dict(r) for r in await c.fetchall()]


db = Database()
