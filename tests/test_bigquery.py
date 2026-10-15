"""Unit tests for BigQuery warehouse client and idempotent upsert logic."""
import datetime
import json
import pytest
from unittest.mock import MagicMock

from src.bigquery_client import BigQueryWarehouse


def test_ensure_dataset_and_table(mock_bigquery_client):
    """Test dataset and table initialization."""
    warehouse = BigQueryWarehouse(client=mock_bigquery_client)

    # Test dataset creation
    warehouse.ensure_dataset()
    mock_bigquery_client.get_dataset.assert_called_once()

    # Test table creation
    table = warehouse.ensure_table("invoices")
    mock_bigquery_client.get_table.assert_called_once()


def test_upsert_records_transformation(mock_bigquery_client):
    """Test transforming QBO records into BigQuery staging rows and executing MERGE."""
    warehouse = BigQueryWarehouse(client=mock_bigquery_client)

    sample_records = [
        {
            "Id": "101",
            "DocNumber": "INV-1001",
            "MetaData": {"LastUpdatedTime": "2026-10-15T12:00:00Z"},
            "Active": True,
        },
        {
            "Id": "102",
            "status": "Deleted",  # CDC deletion
            "MetaData": {"LastUpdatedTime": "2026-10-15T13:00:00Z"},
        },
        {
            "Id": "103",
            "Active": False,  # Soft deletion / inactive master data
            "MetaData": {"LastUpdatedTime": "2026-10-15T14:00:00Z"},
        },
    ]

    sync_time = datetime.datetime(2026, 10, 15, 15, 0, 0, tzinfo=datetime.timezone.utc)
    count = warehouse.upsert_records("Invoice", sample_records, sync_time)

    assert count == 3

    # Verify load_table_from_json was called with correct transformed rows
    mock_bigquery_client.load_table_from_json.assert_called_once()
    args, kwargs = mock_bigquery_client.load_table_from_json.call_args
    staging_rows = args[0]

    assert len(staging_rows) == 3

    # Record 1: Active
    assert staging_rows[0]["id"] == "101"
    assert staging_rows[0]["is_deleted"] is False
    assert staging_rows[0]["last_updated_time"] == "2026-10-15T12:00:00Z"
    # Verify native JSON dict is passed (not double-serialized string)
    assert staging_rows[0]["payload"] == sample_records[0]

    # Record 2: CDC Deleted
    assert staging_rows[1]["id"] == "102"
    assert staging_rows[1]["is_deleted"] is True

    # Record 3: Inactive soft deleted
    assert staging_rows[2]["id"] == "103"
    assert staging_rows[2]["is_deleted"] is True

    # Verify MERGE query was executed
    mock_bigquery_client.query.assert_called_once()
    query_str = mock_bigquery_client.query.call_args[0][0]
    assert "MERGE" in query_str
    assert "T.id = S.id" in query_str
    assert "WHEN MATCHED THEN" in query_str
    assert "WHEN NOT MATCHED THEN" in query_str

    # Verify staging table cleanup
    mock_bigquery_client.delete_table.assert_called_once()


def test_upsert_sandbox_fallback_subsequent_load(mock_bigquery_client):
    """Test BigQuery Sandbox fallback on subsequent load: preserves existing records via UNION ALL and deduplicates."""
    warehouse = BigQueryWarehouse(client=mock_bigquery_client)

    sample_records = [{"Id": "201", "Active": True}]
    sync_time = datetime.datetime(2026, 10, 15, 15, 0, 0, tzinfo=datetime.timezone.utc)

    # First call (MERGE) raises DML sandbox error; second call (dedup CTAS query) succeeds
    mock_query_job_fallback = MagicMock()
    mock_query_job_fallback.result.return_value = None

    mock_bigquery_client.query.side_effect = [
        Exception("DML queries are not supported in the BigQuery Sandbox. Please link a billing account."),
        mock_query_job_fallback,
    ]

    count = warehouse.upsert_records("Bill", sample_records, sync_time)
    assert count == 1

    # Verify fallback query was executed with UNION ALL and deduplication
    assert mock_bigquery_client.query.call_count == 2
    fallback_query_str = mock_bigquery_client.query.call_args[0][0]
    assert "CREATE OR REPLACE TABLE" in fallback_query_str
    assert "ROW_NUMBER() OVER" in fallback_query_str
    assert "UNION ALL" in fallback_query_str
    assert "_rn = 1" in fallback_query_str


def test_upsert_sandbox_fallback_first_load(mock_bigquery_client):
    """Test BigQuery Sandbox fallback on first load when target table does not exist for UNION."""
    warehouse = BigQueryWarehouse(client=mock_bigquery_client)

    sample_records = [{"Id": "301", "Active": True}]
    sync_time = datetime.datetime(2026, 10, 15, 15, 0, 0, tzinfo=datetime.timezone.utc)

    mock_query_job_first_load = MagicMock()
    mock_query_job_first_load.result.return_value = None

    # Call 1: MERGE fails (Sandbox DML error)
    # Call 2: Deduplicating UNION query fails with "Not found" (target table not yet populated/found)
    # Call 3: First-load direct creation query succeeds directly from staging table
    mock_bigquery_client.query.side_effect = [
        Exception("DML queries are not supported in the BigQuery Sandbox."),
        Exception("Not found: Table stone-column-511709-d4.quickbooks_data.bills was not found in location US"),
        mock_query_job_first_load,
    ]

    count = warehouse.upsert_records("Bill", sample_records, sync_time)
    assert count == 1

    assert mock_bigquery_client.query.call_count == 3
    final_query_str = mock_bigquery_client.query.call_args[0][0]
    assert "CREATE OR REPLACE TABLE" in final_query_str
    assert "PARTITION BY DATE(sync_timestamp)" in final_query_str
    assert "CLUSTER BY id" in final_query_str
    assert "UNION ALL" not in final_query_str


def test_upsert_duplicate_ids_retains_latest_timestamp(mock_bigquery_client):
    """Test that duplicate entity IDs in an incoming batch are deduplicated, keeping the latest timestamp."""
    warehouse = BigQueryWarehouse(client=mock_bigquery_client)

    records_with_duplicates = [
        # Older version of Id 500
        {
            "Id": "500",
            "DocNumber": "INV-OLD",
            "MetaData": {"LastUpdatedTime": "2026-10-15T10:00:00Z"},
            "Active": True,
        },
        # Newer version of Id 500
        {
            "Id": "500",
            "DocNumber": "INV-NEW",
            "MetaData": {"LastUpdatedTime": "2026-10-15T12:00:00Z"},
            "Active": True,
        },
        # Record with no timestamp
        {
            "Id": "501",
            "DocNumber": "INV-NO-TS-1",
            "Active": True,
        },
        # Record with timestamp overwriting one without timestamp
        {
            "Id": "501",
            "DocNumber": "INV-WITH-TS-2",
            "MetaData": {"LastUpdatedTime": "2026-10-15T11:00:00Z"},
            "Active": True,
        },
    ]

    sync_time = datetime.datetime(2026, 10, 15, 15, 0, 0, tzinfo=datetime.timezone.utc)
    count = warehouse.upsert_records("Invoice", records_with_duplicates, sync_time)

    # 4 records with 2 unique IDs should result in exactly 2 staged records
    assert count == 2

    args, kwargs = mock_bigquery_client.load_table_from_json.call_args
    staging_rows = args[0]
    assert len(staging_rows) == 2

    # Verify Id 500 kept the newer version
    row_500 = next(r for r in staging_rows if r["id"] == "500")
    assert row_500["payload"]["DocNumber"] == "INV-NEW"
    assert row_500["last_updated_time"] == "2026-10-15T12:00:00Z"

    # Verify Id 501 kept the version with timestamp
    row_501 = next(r for r in staging_rows if r["id"] == "501")
    assert row_501["payload"]["DocNumber"] == "INV-WITH-TS-2"


def test_staging_table_uniqueness_across_batches(mock_bigquery_client):
    """Test that multiple batches in the same sync receive distinct temporary staging table names."""
    warehouse = BigQueryWarehouse(client=mock_bigquery_client)

    sync_time = datetime.datetime(2026, 10, 15, 15, 0, 0, tzinfo=datetime.timezone.utc)

    # Run batch 1
    warehouse.upsert_records("Invoice", [{"Id": "1"}], sync_time)
    staging_table_batch_1 = mock_bigquery_client.load_table_from_json.call_args_list[0][0][1]

    # Run batch 2 with identical sync_time
    warehouse.upsert_records("Invoice", [{"Id": "2"}], sync_time)
    staging_table_batch_2 = mock_bigquery_client.load_table_from_json.call_args_list[1][0][1]

    # Ensure table names are distinct despite identical sync_time
    assert staging_table_batch_1 != staging_table_batch_2
    assert "staging_invoices_" in staging_table_batch_1
    assert "staging_invoices_" in staging_table_batch_2
