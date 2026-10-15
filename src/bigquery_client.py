"""Google BigQuery Client for QuickBooks Online Data Ingestion.

Supports:
- Application Default Credentials (ADC) - No downloaded service-account key required!
- Auto-creation of dataset 'quickbooks_data' and entity tables
- Idempotent UPSERT / MERGE based on entity ID
- Soft-deletion / CDC deletion tracking with '_is_deleted' flag
- Native JSON / structured payload storage for zero schema drift
"""
import datetime
import json
import logging
from typing import Any, Dict, List, Optional
from google.cloud import bigquery
from google.api_core.exceptions import NotFound, Conflict
import google.auth

from src.config import settings

logger = logging.getLogger(__name__)

# Entity name mapping to BigQuery table names
ENTITY_TABLE_MAPPING = {
    "Account": "accounts",
    "Customer": "customers",
    "Vendor": "vendors",
    "Invoice": "invoices",
    "Payment": "payments",
    "Bill": "bills",
    "BillPayment": "bill_payments",
    "JournalEntry": "journal_entries",
}


def _parse_iso_timestamp(ts: Optional[str]) -> datetime.datetime:
    """Safely parses an ISO timestamp string into a timezone-aware UTC datetime.

    Returns datetime.min in UTC if timestamp is missing or malformed.
    """
    if not ts or not isinstance(ts, str):
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    try:
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


class BigQueryWarehouse:
    """Manages dataset/table lifecycle and idempotent data ingestion into BigQuery."""

    def __init__(self, client: Optional[bigquery.Client] = None):
        self.project_id = settings.GCP_PROJECT_ID
        self.dataset_id = settings.BIGQUERY_DATASET
        self.location = settings.BIGQUERY_LOCATION
        self._client = client

    @property
    def client(self) -> bigquery.Client:
        """Lazily returns the BigQuery client, initializing ADC if not already done."""
        if self._client is None:
            self._client = self._init_client()
        return self._client

    def _init_client(self) -> bigquery.Client:
        """Initializes BigQuery client using Application Default Credentials (ADC)."""
        try:
            credentials, default_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            active_project = self.project_id or default_project
            logger.info(f"Connecting to BigQuery using ADC for project: {active_project}")
            return bigquery.Client(project=active_project, credentials=credentials, location=self.location)
        except Exception as e:
            logger.error(
                f"Could not initialize BigQuery client via ADC: {e}. "
                "Ensure you have run `gcloud auth application-default login` for local execution."
            )
            raise

    @property
    def dataset_ref(self) -> bigquery.DatasetReference:
        """Returns the DatasetReference object."""
        return self.client.dataset(self.dataset_id, project=self.project_id)

    def ensure_dataset(self) -> None:
        """Creates the BigQuery dataset if it does not already exist."""
        try:
            self.client.get_dataset(self.dataset_ref)
            logger.info(f"BigQuery dataset '{self.dataset_id}' exists.")
        except NotFound:
            logger.info(f"Dataset '{self.dataset_id}' not found. Creating in location '{self.location}'...")
            dataset = bigquery.Dataset(self.dataset_ref)
            dataset.location = self.location
            dataset.description = "QuickBooks Online ingested data pipeline warehouse"
            self.client.create_dataset(dataset, timeout=30)
            logger.info(f"Dataset '{self.dataset_id}' created successfully.")

    def ensure_table(self, table_name: str) -> bigquery.Table:
        """Ensures that the target table exists with the proper schema and clustering."""
        self.ensure_dataset()
        table_ref = self.dataset_ref.table(table_name)

        try:
            return self.client.get_table(table_ref)
        except NotFound:
            logger.info(f"Creating table '{table_name}' in dataset '{self.dataset_id}'...")
            schema = [
                bigquery.SchemaField("id", "STRING", mode="REQUIRED", description="QuickBooks Entity ID"),
                bigquery.SchemaField("sync_timestamp", "TIMESTAMP", mode="REQUIRED", description="Pipeline ingestion timestamp"),
                bigquery.SchemaField("last_updated_time", "TIMESTAMP", mode="NULLABLE", description="QuickBooks MetaData.LastUpdatedTime"),
                bigquery.SchemaField("is_deleted", "BOOLEAN", mode="REQUIRED", description="True if marked inactive or deleted in QBO"),
                bigquery.SchemaField("payload", "JSON", mode="NULLABLE", description="Full raw QuickBooks JSON entity object"),
            ]
            table = bigquery.Table(table_ref, schema=schema)
            # Partitioning by day of sync timestamp enables efficient data lifecycle management
            table.time_partitioning = bigquery.TimePartitioning(
                type_=bigquery.TimePartitioningType.DAY,
                field="sync_timestamp",
            )
            # Clustering by id optimizes merge and lookup performance
            table.clustering_fields = ["id"]

            created_table = self.client.create_table(table)
            logger.info(f"Table '{table_name}' created successfully.")
            return created_table

    def upsert_records(self, entity_name: str, records: List[Dict[str, Any]], sync_time: datetime.datetime) -> int:
        """Idempotently upserts (MERGES) records into the entity's BigQuery table.

        - Deduplicates incoming records by entity ID, keeping the record with the latest LastUpdatedTime.
        - If record exists in table: updates last_updated_time, sync_timestamp, is_deleted, and payload.
        - If record is new: inserts the new row.
        - Returns number of records processed.
        """
        if not records:
            return 0

        table_name = ENTITY_TABLE_MAPPING.get(entity_name, entity_name.lower())
        self.ensure_table(table_name)

        # 1. Deduplicate incoming records by entity ID before staging.
        # When duplicate IDs exist, retain the record with the latest MetaData.LastUpdatedTime.
        records_by_id: Dict[str, Dict[str, Any]] = {}
        for r in records:
            entity_id = str(r.get("Id", "")).strip()
            if not entity_id:
                continue

            if entity_id not in records_by_id:
                records_by_id[entity_id] = r
            else:
                existing_record = records_by_id[entity_id]
                existing_meta = existing_record.get("MetaData") if isinstance(existing_record, dict) else {}
                existing_ts = existing_meta.get("LastUpdatedTime") if isinstance(existing_meta, dict) else None

                new_meta = r.get("MetaData") if isinstance(r, dict) else {}
                new_ts = new_meta.get("LastUpdatedTime") if isinstance(new_meta, dict) else None

                # Keep the record with the latest timestamp; if timestamps are equal or missing, keep latest seen
                if _parse_iso_timestamp(new_ts) >= _parse_iso_timestamp(existing_ts):
                    records_by_id[entity_id] = r

        deduped_records = list(records_by_id.values())
        if not deduped_records:
            return 0

        # Transform raw QBO entity records into staging rows
        staging_rows = []
        for r in deduped_records:
            entity_id = str(r.get("Id", ""))

            # Detect deletion:
            # 1. Transaction deletion via CDC: status == 'Deleted'
            # 2. Master entity soft-deletion: Active == False
            status = r.get("status")
            is_active = r.get("Active", True)
            is_deleted = (str(status).lower() == "deleted") or (is_active is False)

            # Extract QuickBooks LastUpdatedTime if present
            last_updated = None
            metadata = r.get("MetaData", {})
            if isinstance(metadata, dict):
                last_updated = metadata.get("LastUpdatedTime")

            staging_rows.append({
                "id": entity_id,
                "sync_timestamp": sync_time.isoformat(),
                "last_updated_time": last_updated,
                "is_deleted": is_deleted,
                "payload": r,
            })

        import uuid
        batch_id = uuid.uuid4().hex[:8]
        target_table_id = f"{self.project_id}.{self.dataset_id}.{table_name}"
        temp_table_name = f"staging_{table_name}_{int(sync_time.timestamp())}_{batch_id}"
        temp_table_id = f"{self.project_id}.{self.dataset_id}.{temp_table_name}"

        # 1. Load batch into temporary staging table
        job_config = bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
                bigquery.SchemaField("sync_timestamp", "TIMESTAMP", mode="REQUIRED"),
                bigquery.SchemaField("last_updated_time", "TIMESTAMP", mode="NULLABLE"),
                bigquery.SchemaField("is_deleted", "BOOLEAN", mode="REQUIRED"),
                bigquery.SchemaField("payload", "JSON", mode="NULLABLE"),
            ],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )

        logger.info(f"Loading {len(staging_rows)} records into staging table {temp_table_id}...")
        load_job = self.client.load_table_from_json(staging_rows, temp_table_id, job_config=job_config)
        load_job.result()  # Wait for staging load to finish

        # 2. Execute idempotent MERGE from staging to target table
        merge_sql = f"""
        MERGE `{target_table_id}` T
        USING `{temp_table_id}` S
        ON T.id = S.id
        WHEN MATCHED THEN
          UPDATE SET
            T.sync_timestamp = S.sync_timestamp,
            T.last_updated_time = S.last_updated_time,
            T.is_deleted = S.is_deleted,
            T.payload = S.payload
        WHEN NOT MATCHED THEN
          INSERT (id, sync_timestamp, last_updated_time, is_deleted, payload)
          VALUES (S.id, S.sync_timestamp, S.last_updated_time, S.is_deleted, S.payload);
        """

        logger.info(f"Executing BigQuery MERGE for {target_table_id}...")
        try:
            query_job = self.client.query(merge_sql)
            query_job.result()  # Wait for merge to complete
        except Exception as e:
            err_str = str(e).lower()
            # BigQuery Sandbox without billing does not support DML (MERGE, UPDATE, DELETE).
            # Fall back to DDL CTAS with deduplication, which is 100% supported in Sandbox.
            if "dml" in err_str or "sandbox" in err_str or "billing" in err_str:
                logger.warning(
                    f"BigQuery Sandbox detected (DML not supported). "
                    f"Executing CTAS deduplication fallback for {target_table_id}..."
                )
                sandbox_dedup_sql = f"""
                CREATE OR REPLACE TABLE `{target_table_id}`
                PARTITION BY DATE(sync_timestamp)
                CLUSTER BY id AS
                SELECT id, sync_timestamp, last_updated_time, is_deleted, payload
                FROM (
                  SELECT id, sync_timestamp, last_updated_time, is_deleted, payload,
                         ROW_NUMBER() OVER (
                           PARTITION BY id
                           ORDER BY sync_timestamp DESC, last_updated_time DESC NULLS LAST
                         ) as _rn
                  FROM (
                    SELECT id, sync_timestamp, last_updated_time, is_deleted, payload FROM `{target_table_id}`
                    UNION ALL
                    SELECT id, sync_timestamp, last_updated_time, is_deleted, payload FROM `{temp_table_id}`
                  )
                )
                WHERE _rn = 1;
                """
                try:
                    # Attempt subsequent-load deduplicating CTAS query (preserves existing target records)
                    query_job = self.client.query(sandbox_dedup_sql)
                    query_job.result()
                except Exception as fallback_err:
                    err_msg = str(fallback_err).lower()
                    # If target table does not exist yet (first sync), create directly from staging table
                    if "not found" in err_msg or "not_found" in err_msg:
                        logger.info(
                            f"Target table {target_table_id} not found for UNION in Sandbox fallback. "
                            f"Creating directly from staging table {temp_table_id} for first load..."
                        )
                        first_load_sql = f"""
                        CREATE OR REPLACE TABLE `{target_table_id}`
                        PARTITION BY DATE(sync_timestamp)
                        CLUSTER BY id AS
                        SELECT id, sync_timestamp, last_updated_time, is_deleted, payload
                        FROM `{temp_table_id}`;
                        """
                        query_job = self.client.query(first_load_sql)
                        query_job.result()
                    else:
                        raise
            else:
                raise

        # 3. Clean up staging table
        try:
            self.client.delete_table(temp_table_id, not_found_ok=True)
            logger.info(f"Cleaned up staging table {temp_table_id}.")
        except Exception as e:
            logger.warning(f"Could not delete temporary staging table {temp_table_id}: {e}")

        logger.info(f"Successfully upserted {len(staging_rows)} records into {target_table_id}.")
        return len(staging_rows)


# Default warehouse instance
bigquery_warehouse = BigQueryWarehouse()
