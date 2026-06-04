# 🤖 IndTech TradeX Pro — Telegram Trading Bot

Binance Futures auto-trading bot powered by the **IndTech TradeX Pro** strategy.
No TradingView needed — strategy runs entirely inside Python.

---

## 📁 Project Structure

```
tradex_v2/
├── bot.py              # Entry point — starts bot + scheduler
├── config.py           # All settings (loaded from .env)
├── database.py         # SQLite async DB (users, trades, signals)
├── strategy.py         # Full IndTech TradeX strategy engine
├── binance_engine.py   # Binance Futures execution layer
├── scanner.py          # Signal scanner (runs every N minutes)
├── trade_monitor.py    # Open trade monitor (TSL, flip exit, TP/SL detect)
├── handlers/
│   ├── user.py         # User Telegram UI (buttons, settings, portfolio)
│   └── admin.py        # Admin panel (approve, ban, subscriptions)
├── requirements.txt
└── .env.example
```

---

## ⚡ Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
nano .env
```

Fill in:
| Key | Description |
|-----|-------------|
| `BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `ADMIN_IDS` | Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot)) |
| `BINANCE_API_KEY` | Optional master key for market data (public endpoints work without) |
| `WEBHOOK_SECRET` | Not needed — bot uses internal strategy |

### 3. Run
```bash
python bot.py
```

---

## 📊 Strategy Logic (IndTech TradeX Pro)

| Component | Logic |
|-----------|-------|
| **Signal** | Supertrend direction flip on confirmed bar |
| **Stop Loss** | `low/high ± ATR × sl_buffer` (wick protection) |
| **Safe SL** | Last SMC pivot low/high (used for actual order) |
| **Take Profit** | `Risk × RR ratio` or `entry ± %` |
| **DCA Price** | FVG midpoint / Fibonacci 61.8% / 50% midpoint |
| **MTF Filter** | 5m + 15m + 1h + 4h + 1D (Supertrend + EMA200 + RSI) |
| **Trailing SL** | SMC swing point + ATR buffer, moves in profit direction only |

---

## 🔄 Trade Exit Logic (4 mechanisms)

| # | Exit | How |
|---|------|-----|
| 1 | 🎯 **Take Profit** | Binance stop order fills automatically |
| 2 | 🛑 **Stop Loss** | Binance stop order fills automatically |
| 3 | 🔒 **Trailing SL** | Bot updates SL order every minute as price moves |
| 4 | 🔄 **Supertrend Flip** | Bot detects trend reversal → closes at market |

---

## 👤 User Flow

```
1. User sends /start
2. Bot saves them as "pending" → notifies admins
3. Admin opens /admin → Pending → approves user (grants 30-day sub)
4. User receives approval notification
5. User goes to ⚙️ Settings → 🔑 API Keys
6. User enters Binance Futures API key + secret
   (secret message auto-deleted for security)
7. User enables 🤖 Auto Trade
8. Bot scans markets, detects signals, executes trades automatically
9. Bot monitors open trades, updates trailing SL, exits on flip/TP/SL
```

---

## 🛠 Admin Commands

| Command | Description |
|---------|-------------|
| `/admin` | Open admin panel |

**Admin Panel features:**
- View pending / active / banned users
- Approve users (grants 30-day subscription)
- Ban / unban users
- Grant +7 / +30 / +90 day subscriptions
- Promote users to admin
- View last signals and bot stats
- Broadcast message to all active users

---

## ⚙️ User Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Leverage | 10x | Futures leverage |
| Position Size | 5% | % of available balance per trade |
| TP R:R | 2.0 | Take profit = risk × ratio |
| DCA Method | FVG | FVG / Fibonacci 61.8% / 50% midpoint |
| Auto Trade | OFF | Toggle to enable auto-execution |

---

## 🔒 Security Notes

- Each user provides their **own** Binance API keys
- API secret messages are **auto-deleted** from Telegram after saving
- Keys are stored in local SQLite DB on your server
- Binance API keys should have **NO withdrawal permission**
- Set **IP whitelist** on Binance API keys to your server IP

---

## 🚀 Production (systemd)

```ini
# /etc/systemd/system/tradex.service
[Unit]
Description=IndTech TradeX Pro Bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/tradex_v2
ExecStart=/usr/bin/python3 bot.py
Restart=always
User=ubuntu
EnvironmentFile=/home/ubuntu/tradex_v2/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable tradex
sudo systemctl start tradex
sudo journalctl -u tradex -f
```
