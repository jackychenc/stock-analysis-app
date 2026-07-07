"""APScheduler entrypoint — daily batch at 03:00 Taiwan time (NFR-01 window).

Run: python -m app.batch.scheduler
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.batch.pipeline import run_pipeline_once

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_pipeline_once,
        CronTrigger(hour=3, minute=0, timezone="Asia/Taipei"),
        id="daily-batch",
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("batch scheduler started; daily run at 03:00 Asia/Taipei")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
