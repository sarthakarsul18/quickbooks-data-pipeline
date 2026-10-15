import argparse
import logging
import sys

from src.auth import auth_manager
from src.config import settings
from src.extractor import QuickBooksExtractor


def setup_logging():
    """Sets up a clean console logging format."""
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    """Main execution function for python -m src.sync."""
    parser = argparse.ArgumentParser(
        description="QuickBooks Online Data Pipeline Extractor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force a full sync of all entities even if previous sync checkpoint exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Safely test QuickBooks queries for all 8 entities and BigQuery ADC connection without writing records.",
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("src.sync")

    logger.info("Starting QuickBooks Data Pipeline extraction job...")
    logger.info(f"Target BigQuery Dataset: {settings.GCP_PROJECT_ID}.{settings.BIGQUERY_DATASET}")

    # Check OAuth authentication status
    if not auth_manager.is_authenticated():
        logger.error(
            "QuickBooks Online is NOT authenticated!\n"
            "Please start the OAuth web server using:\n"
            "    python -m uvicorn src.api:app --reload\n"
            "and navigate to http://localhost:8000/auth/login to authenticate with your Sandbox."
        )
        sys.exit(1)

    extractor = QuickBooksExtractor()

    if args.dry_run:
        logger.info("Running in DRY-RUN mode (safe verification only, no records written to BigQuery).")
        report = extractor.dry_run()
        logger.info("================ DRY RUN SUMMARY ================")
        logger.info(f"Overall Status: {report['status'].upper()}")
        logger.info(f"BigQuery ADC:   {report['bigquery_adc']}")
        logger.info("QuickBooks Entities Verified:")
        for entity, info in report["quickbooks_connectivity"].items():
            status = info.get("status")
            sample_id = info.get("sample_id", "None")
            logger.info(f"  - {entity.ljust(15)}: {status} (Sample ID: {sample_id})")
        logger.info("=================================================")
        return

    try:
        results = extractor.sync(force_full=args.full)
        logger.info("================ SYNC SUMMARY ================")
        logger.info(f"Sync Type:     {results['sync_type'].upper()}")
        logger.info(f"Status:        {results['status']}")
        logger.info(f"Timestamp:     {results['start_time']}")
        logger.info(f"Total Records: {results['total_records']}")
        for entity, count in results["entity_counts"].items():
            logger.info(f"  - {entity.ljust(15)}: {count} records")
        logger.info("==============================================")
    except Exception as e:
        logger.exception(f"Pipeline extraction failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
