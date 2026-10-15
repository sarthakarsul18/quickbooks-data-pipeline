"""Automated Scheduler for QuickBooks Online Data Pipeline.

Runs the sync job automatically every 4 hours (configurable via SYNC_INTERVAL_HOURS).

Usage:
    python -m src.scheduler
"""
import datetime
import logging
import signal
import sys
from apscheduler.schedulers.blocking import BlockingScheduler

from src.auth import auth_manager
from src.config import settings
from src.extractor import QuickBooksExtractor

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [Scheduler] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("src.scheduler")


def run_scheduled_sync():
    """Job function executed on every scheduler interval."""
    logger.info("Triggered scheduled QuickBooks sync job.")

    if not auth_manager.is_authenticated():
        logger.error(
            "Skipping scheduled sync: Pipeline is not authenticated. "
            "Please authenticate at http://localhost:8000/auth/login."
        )
        return

    try:
        extractor = QuickBooksExtractor()
        results = extractor.sync(force_full=False)
        logger.info(
            f"Scheduled sync completed successfully: {results['total_records']} total records processed "
            f"({results['sync_type']})."
        )
    except Exception as e:
        logger.exception(f"Scheduled sync encountered an error: {e}")


def main():
    """Initializes and runs the APScheduler blocking loop."""
    interval_hours = settings.SYNC_INTERVAL_HOURS
    logger.info("================ STARTING SCHEDULER ================")
    logger.info(f"Target BigQuery Dataset: {settings.GCP_PROJECT_ID}.{settings.BIGQUERY_DATASET}")
    logger.info(f"Sync Interval: Every {interval_hours} hours.")

    scheduler = BlockingScheduler()

    # Schedule the recurring job every N hours
    scheduler.add_job(
        run_scheduled_sync,
        trigger="interval",
        hours=interval_hours,
        id="qbo_sync_job",
        next_run_time=datetime.datetime.now(),  # Trigger immediately on initial start
    )

    def shutdown(signum, frame):
        logger.info("Shutdown signal received. Shutting down scheduler gracefully...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
