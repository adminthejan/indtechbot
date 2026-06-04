"""
IndTech TradeX Pro Bot — Entry Point
=====================================
Starts:
  1. Telegram bot (polling)
  2. Signal scanner (every SCAN_INTERVAL_MINUTES)
  3. Trade monitor (every MONITOR_INTERVAL_MINUTES)
"""

import logging
import os

from telegram.ext import Application
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, ADMIN_IDS, SCAN_INTERVAL_MINUTES, MONITOR_INTERVAL_MINUTES
from database import db
from handlers.user import register_user_handlers
from handlers.admin import register_admin_handlers
from scanner import run_scan
from trade_monitor import run_monitor

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("tradex.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    await db.init()
    for admin_id in ADMIN_IDS:
        await db.ensure_admin(admin_id)
    logger.info("DB ready. Admins: %s", ADMIN_IDS)

    # Start scheduler inside the running event loop
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_scan,
        trigger="interval",
        minutes=SCAN_INTERVAL_MINUTES,
        args=[application],
        id="scanner",
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        run_monitor,
        trigger="interval",
        minutes=MONITOR_INTERVAL_MINUTES,
        args=[application],
        id="monitor",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started — scanner every %dm, monitor every %dm",
        SCAN_INTERVAL_MINUTES, MONITOR_INTERVAL_MINUTES,
    )


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    register_admin_handlers(application)
    register_user_handlers(application)

    logger.info("IndTech TradeX Pro Bot starting...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
