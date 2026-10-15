"""QuickBooks Online Data Extractor.

Orchestrates data extraction for the 8 core accounting entities:
- Account
- Customer
- Vendor
- Invoice
- Payment
- Bill
- BillPayment
- JournalEntry

Supports:
- Initial Full Sync via paginated queries
- Incremental Sync via Change Data Capture (CDC) API
- Fallback to MetaData.LastUpdatedTime query when CDC is unavailable
- Tracking updates and soft/hard deletions
- Batching and loading records into Google BigQuery
"""
import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from google.api_core.exceptions import NotFound

from src.bigquery_client import BigQueryWarehouse, bigquery_warehouse
from src.client import QuickBooksClient
from src.config import settings

logger = logging.getLogger(__name__)

# The 8 QuickBooks entities required for the data pipeline
SUPPORTED_ENTITIES = [
    "Account",
    "Customer",
    "Vendor",
    "Invoice",
    "Payment",
    "Bill",
    "BillPayment",
    "JournalEntry",
]


class QuickBooksExtractor:
    """Coordinates extraction of QuickBooks entities and ingestion into BigQuery."""

    def __init__(
        self,
        client: Optional[QuickBooksClient] = None,
        warehouse: Optional[BigQueryWarehouse] = None,
        state_file: Optional[Path] = None,
    ):
        self.client = client or QuickBooksClient()
        self.warehouse = warehouse or bigquery_warehouse
        self.state_file = state_file or settings.STATE_FILE_PATH

    def load_last_sync_time(self) -> Optional[datetime.datetime]:
        """Reads the timestamp of the last successful sync from the state file."""
        if not self.state_file.exists():
            return None

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                last_time_str = data.get("last_successful_sync")
                if last_time_str:
                    dt = datetime.datetime.fromisoformat(last_time_str)
                    # Normalize to UTC timezone to prevent offset-naive vs offset-aware errors
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    else:
                        dt = dt.astimezone(datetime.timezone.utc)
                    return dt
        except Exception as e:
            logger.warning(f"Failed to read sync state file {self.state_file}: {e}")
        return None

    def save_last_sync_time(self, sync_time: datetime.datetime) -> None:
        """Saves the last successful sync timestamp atomically to the state file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        # Ensure timestamp is UTC
        utc_time = sync_time.astimezone(datetime.timezone.utc) if sync_time.tzinfo else sync_time.replace(tzinfo=datetime.timezone.utc)
        data = {
            "last_successful_sync": utc_time.isoformat(),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        # Atomic write: write to temp file then replace
        temp_state_file = self.state_file.with_suffix(".tmp")
        with open(temp_state_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_state_file.replace(self.state_file)
        logger.info(f"Updated sync state to: {utc_time.isoformat()}")

    def run_full_sync(self, sync_time: datetime.datetime) -> Dict[str, int]:
        """Performs a full extraction for all 8 entities using paginated SELECT queries."""
        logger.info("================ STARTING FULL SYNC ================")
        entity_counts: Dict[str, int] = {}

        for entity in SUPPORTED_ENTITIES:
            logger.info(f"Extracting full dataset for entity: {entity}...")
            batch: List[Dict[str, Any]] = []
            total_for_entity = 0

            for record in self.client.query(entity):
                batch.append(record)
                if len(batch) >= 500:
                    self.warehouse.upsert_records(entity, batch, sync_time)
                    total_for_entity += len(batch)
                    batch = []

            if batch:
                self.warehouse.upsert_records(entity, batch, sync_time)
                total_for_entity += len(batch)

            entity_counts[entity] = total_for_entity
            logger.info(f"Finished extracting {entity}: {total_for_entity} records processed.")

        logger.info("================ FULL SYNC COMPLETED ================")
        return entity_counts

    def run_incremental_sync(
        self, last_sync_time: datetime.datetime, sync_time: datetime.datetime
    ) -> Dict[str, int]:
        """Performs an incremental extraction using the QuickBooks CDC endpoint.

        Falls back to LastUpdatedTime queries if CDC returns an error or window exceeds 30 days.
        """
        logger.info(f"================ STARTING INCREMENTAL SYNC (since {last_sync_time.isoformat()}) ================")

        # Check CDC 30-day limitation
        days_since_last_sync = (sync_time - last_sync_time).total_seconds() / 86400
        if days_since_last_sync >= 29.5:
            logger.warning(
                f"Sync gap is {days_since_last_sync:.1f} days, which approaches or exceeds QuickBooks CDC 30-day limit. "
                "Triggering full sync to ensure complete data integrity."
            )
            return self.run_full_sync(sync_time)

        entity_counts: Dict[str, int] = {}
        # QuickBooks CDC expects changedSince in ISO 8601 UTC format (e.g. 2026-10-15T10:00:00Z)
        changed_since_str = last_sync_time.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            cdc_results = self.client.get_cdc(SUPPORTED_ENTITIES, changed_since_str)
            for entity, records in cdc_results.items():
                if records:
                    self.warehouse.upsert_records(entity, records, sync_time)
                entity_counts[entity] = len(records)
                logger.info(f"Incremental sync for {entity}: {len(records)} records processed.")
        except Exception as e:
            logger.warning(
                f"CDC query failed: {e}. Falling back to MetaData.LastUpdatedTime incremental query with 60-second overlap."
            )
            # 60-second overlap lookback buffer prevents missing boundary updates
            overlap_time = last_sync_time - datetime.timedelta(seconds=60)
            overlap_str = overlap_time.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            for entity in SUPPORTED_ENTITIES:
                where_clause = f"MetaData.LastUpdatedTime >= '{overlap_str}'"
                batch = []
                total = 0
                for record in self.client.query(entity, where_clause=where_clause):
                    batch.append(record)
                    if len(batch) >= 500:
                        self.warehouse.upsert_records(entity, batch, sync_time)
                        total += len(batch)
                        batch = []
                if batch:
                    self.warehouse.upsert_records(entity, batch, sync_time)
                    total += len(batch)
                entity_counts[entity] = total

        logger.info("================ INCREMENTAL SYNC COMPLETED ================")
        return entity_counts

    def dry_run(self) -> Dict[str, Any]:
        """Safely verifies QBO API connection and BigQuery ADC dataset without writing records or metadata."""
        logger.info("================ STARTING DRY RUN VERIFICATION ================")
        report: Dict[str, Any] = {
            "quickbooks_connectivity": {},
            "bigquery_adc": "unverified",
            "status": "success",
        }

        # 1. Test sample queries for all 8 entities (read-only 1 record each)
        for entity in SUPPORTED_ENTITIES:
            try:
                # Query a single record using generator
                sample = next(self.client.query(entity), None)
                report["quickbooks_connectivity"][entity] = {
                    "status": "ok",
                    "sample_found": sample is not None,
                    "sample_id": sample.get("Id") if sample else None,
                }
                logger.info(f"Dry run query {entity}: OK (Sample Id: {sample.get('Id') if sample else 'None'})")
            except Exception as e:
                report["quickbooks_connectivity"][entity] = {"status": "error", "error": str(e)}
                report["status"] = "partial_failure"
                logger.error(f"Dry run query {entity} failed: {e}")

        # 2. Read-only verification of BigQuery ADC credentials and dataset existence without creating it
        try:
            self.warehouse.client.get_dataset(self.warehouse.dataset_ref)
            report["bigquery_adc"] = "ok (dataset exists)"
            logger.info("Dry run BigQuery ADC check: OK (dataset exists)")
        except NotFound:
            # ADC credentials are valid, but dataset has not been created yet
            report["bigquery_adc"] = "ok (ADC valid; dataset does not exist yet and will be created on first sync)"
            logger.info("Dry run BigQuery ADC check: OK (ADC valid; dataset will be created on first sync)")
        except Exception as e:
            report["bigquery_adc"] = f"failed: {e}"
            report["status"] = "partial_failure"
            logger.error(f"Dry run BigQuery check failed: {e}")

        logger.info("================ DRY RUN COMPLETED ================")
        return report

    def sync(self, force_full: bool = False) -> Dict[str, Any]:
        """Executes sync workflow (either full or incremental based on state)."""
        sync_start_time = datetime.datetime.now(datetime.timezone.utc)
        last_sync = self.load_last_sync_time()

        if force_full or last_sync is None:
            sync_type = "full"
            counts = self.run_full_sync(sync_start_time)
        else:
            sync_type = "incremental"
            counts = self.run_incremental_sync(last_sync, sync_start_time)

        # Update checkpoint state upon successful completion
        self.save_last_sync_time(sync_start_time)

        total_records = sum(counts.values())
        return {
            "sync_type": sync_type,
            "status": "success",
            "start_time": sync_start_time.isoformat(),
            "total_records": total_records,
            "entity_counts": counts,
        }
