import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
BOT_TOKEN: str        = os.getenv("BOT_TOKEN", "YOUR_TOKEN")
_raw                  = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: list[int]  = [int(x.strip()) for x in _raw.split(",") if x.strip()]

# Binance
BINANCE_API_KEY: str    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
BINANCE_TESTNET: bool   = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

# Strategy
ST_ATR_PERIOD: int    = int(os.getenv("ST_ATR_PERIOD", "10"))
ST_MULTIPLIER: float  = float(os.getenv("ST_MULTIPLIER", "3.0"))
SL_BUFFER_ATR: float  = float(os.getenv("SL_BUFFER_ATR", "0.7"))
TSL_BUFFER_ATR: float = float(os.getenv("TSL_BUFFER_ATR", "0.5"))
TP_RR_RATIO: float    = float(os.getenv("TP_RR_RATIO", "2.0"))
TP_MODE: str          = os.getenv("TP_MODE", "RR")
TP_PERCENT: float     = float(os.getenv("TP_PERCENT", "1.0"))
DCA_METHOD: str       = os.getenv("DCA_METHOD", "FVG")
MTF_PIVOT_LEN: int    = int(os.getenv("MTF_PIVOT_LEN", "5"))

# Scanner
SCAN_INTERVAL_MINUTES: int  = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
SCAN_TIMEFRAME: str         = os.getenv("SCAN_TIMEFRAME", "15m")
SCAN_SYMBOLS: list[str]     = [s.strip() for s in os.getenv(
    "SCAN_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT"
).split(",") if s.strip()]
MTF_CONFIRM_MIN_SCORE: int  = int(os.getenv("MTF_CONFIRM_MIN_SCORE", "3"))

# Monitor
MONITOR_INTERVAL_MINUTES: int = int(os.getenv("MONITOR_INTERVAL_MINUTES", "1"))

# Trade defaults
DEFAULT_LEVERAGE: int        = int(os.getenv("DEFAULT_LEVERAGE", "10"))
DEFAULT_MARGIN_TYPE: str     = os.getenv("DEFAULT_MARGIN_TYPE", "ISOLATED")
DEFAULT_POSITION_SIZE: float = float(os.getenv("DEFAULT_POSITION_SIZE", "5.0"))
MAX_OPEN_TRADES: int         = int(os.getenv("MAX_OPEN_TRADES", "5"))
