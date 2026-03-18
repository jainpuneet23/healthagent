"""
scheduler.py — Daily cron job runner.

On Railway: this runs as a separate process defined in Procfile.
It uses APScheduler to trigger health analysis at 8:00 AM every day
(giving the Health Auto Export iOS app time to push data first at 7:00 AM).
"""

import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def run_daily_analysis():
    logger.info("Starting daily health analysis...")
    from health_agent import run_all_users
    run_all_users()
    logger.info("Daily health analysis complete.")


if __name__ == "__main__":
    scheduler = BlockingScheduler(timezone="Asia/Kolkata")  # IST — change if needed

    # Run every day at 08:00 AM
    scheduler.add_job(
        run_daily_analysis,
        trigger=CronTrigger(hour=8, minute=0),
        id="daily_health_analysis",
        name="Daily health analysis",
        replace_existing=True,
    )

    logger.info("Scheduler started. Analysis will run daily at 08:00 AM IST.")

    # Run once immediately on startup so you see output right away
    try:
        run_daily_analysis()
    except Exception as e:
        logger.warning(f"Initial run skipped or failed: {e}")

    scheduler.start()
