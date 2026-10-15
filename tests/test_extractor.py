import datetime
import pytest
from unittest.mock import MagicMock
from google.api_core.exceptions import NotFound

from src.extractor import QuickBooksExtractor, SUPPORTED_ENTITIES


@pytest.fixture
def mock_client():
    client = MagicMock()
    return client


@pytest.fixture
def mock_warehouse():
    warehouse = MagicMock()
    warehouse.upsert_records.return_value = 5
    return warehouse


def test_sync_state_persistence(tmp_path):
    """Test reading and writing checkpoint timestamp."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(state_file=state_file)

    assert extractor.load_last_sync_time() is None

    test_time = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    extractor.save_last_sync_time(test_time)

    loaded_time = extractor.load_last_sync_time()
    assert loaded_time == test_time


def test_run_full_sync(mock_client, mock_warehouse, tmp_path):
    """Test full sync queries all 8 entities and loads to warehouse."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    # Return sample records for each entity query
    mock_client.query.return_value = [{"Id": "1"}, {"Id": "2"}]

    sync_time = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    counts = extractor.run_full_sync(sync_time)

    # Must extract all 8 entities
    assert len(counts) == 8
    for entity in SUPPORTED_ENTITIES:
        assert entity in counts
        assert counts[entity] == 2

    # Warehouse upsert should be called for each entity
    assert mock_warehouse.upsert_records.call_count == 8


def test_run_incremental_sync(mock_client, mock_warehouse, tmp_path):
    """Test incremental sync triggers CDC query and loads changed records."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    last_sync = datetime.datetime(2026, 10, 15, 8, 0, 0, tzinfo=datetime.timezone.utc)
    current_sync = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)

    mock_client.get_cdc.return_value = {
        "Invoice": [{"Id": "101"}, {"Id": "102", "status": "Deleted"}],
        "Customer": [{"Id": "1"}],
        "Account": [],
        "Vendor": [],
        "Payment": [],
        "Bill": [],
        "BillPayment": [],
        "JournalEntry": [],
    }

    counts = extractor.run_incremental_sync(last_sync, current_sync)

    assert counts["Invoice"] == 2
    assert counts["Customer"] == 1
    assert counts["Account"] == 0
    # Upsert called for entities with records
    assert mock_warehouse.upsert_records.call_count == 2


def test_incremental_sync_fallback_on_30_day_limit(mock_client, mock_warehouse, tmp_path):
    """Test fallback to full sync when gap exceeds 30 days due to CDC limitation."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    # Gap of 35 days
    last_sync = datetime.datetime(2026, 9, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    current_sync = datetime.datetime(2026, 10, 15, 0, 0, 0, tzinfo=datetime.timezone.utc)

    mock_client.query.return_value = [{"Id": "1"}]

    counts = extractor.run_incremental_sync(last_sync, current_sync)

    # Verifies it reverted to full sync query
    assert mock_client.query.call_count == 8
    assert len(counts) == 8


def test_incremental_sync_fallback_uses_60_second_overlap(mock_client, mock_warehouse, tmp_path):
    """Test fallback query uses a 60-second overlap buffer when CDC fails."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    last_sync = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    current_sync = datetime.datetime(2026, 10, 15, 14, 0, 0, tzinfo=datetime.timezone.utc)

    # Force CDC failure to trigger fallback
    mock_client.get_cdc.side_effect = Exception("CDC endpoint temporarily unavailable")
    mock_client.query.return_value = [{"Id": "1"}]

    counts = extractor.run_incremental_sync(last_sync, current_sync)

    assert len(counts) == 8
    # Overlap buffer is 60 seconds before 12:00:00 -> 11:59:00
    expected_clause = "MetaData.LastUpdatedTime >= '2026-10-15T11:59:00Z'"
    for call in mock_client.query.call_args_list:
        assert call[1]["where_clause"] == expected_clause


def test_dry_run_success_dataset_exists(mock_client, mock_warehouse, tmp_path):
    """Test extractor dry_run verifies all 8 entities and BigQuery when dataset exists."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    mock_client.query.side_effect = lambda entity, **kw: iter([{"Id": f"{entity}_123"}])
    mock_warehouse.client.get_dataset.return_value = MagicMock()

    report = extractor.dry_run()

    assert report["status"] == "success"
    assert "ok (dataset exists)" in report["bigquery_adc"]
    assert len(report["quickbooks_connectivity"]) == 8
    for entity in SUPPORTED_ENTITIES:
        assert report["quickbooks_connectivity"][entity]["status"] == "ok"
        assert report["quickbooks_connectivity"][entity]["sample_id"] == f"{entity}_123"

    # Confirms NO records were upserted and state file was NOT created/modified
    assert mock_warehouse.upsert_records.call_count == 0
    assert not state_file.exists()


def test_dry_run_when_dataset_missing_does_not_create(mock_client, mock_warehouse, tmp_path):
    """Test extractor dry_run handles missing dataset without creating it in BigQuery."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    mock_client.query.side_effect = lambda entity, **kw: iter([{"Id": f"{entity}_123"}])
    # Simulate dataset not existing in BigQuery
    mock_warehouse.client.get_dataset.side_effect = NotFound("Dataset quickbooks_data not found")

    report = extractor.dry_run()

    assert report["status"] == "success"
    assert "will be created on first sync" in report["bigquery_adc"]

    # Confirms create_dataset was NEVER called during dry-run
    assert mock_warehouse.client.create_dataset.call_count == 0
    assert mock_warehouse.upsert_records.call_count == 0
    assert not state_file.exists()


def test_fault_failure_preserves_checkpoint_state(mock_client, mock_warehouse, tmp_path):
    """Test that if CDC and fallback query encounter API Faults or errors, sync fails and checkpoint is not updated."""
    state_file = tmp_path / "sync_state.json"
    extractor = QuickBooksExtractor(client=mock_client, warehouse=mock_warehouse, state_file=state_file)

    initial_checkpoint = datetime.datetime(2026, 10, 15, 10, 0, 0, tzinfo=datetime.timezone.utc)
    extractor.save_last_sync_time(initial_checkpoint)

    # Simulate CDC returning a Fault or error
    mock_client.get_cdc.side_effect = RuntimeError("QuickBooks API Fault received on cdc: [Error 5000: Invalid Token]")
    # Simulate fallback query also failing due to Fault or error
    mock_client.query.side_effect = RuntimeError("QuickBooks API Fault received on query: [Error 5000: Invalid Token]")

    with pytest.raises(RuntimeError, match="QuickBooks API Fault"):
        extractor.sync()

    # Verify checkpoint was preserved and NOT updated
    loaded_checkpoint = extractor.load_last_sync_time()
    assert loaded_checkpoint == initial_checkpoint
    assert mock_warehouse.upsert_records.call_count == 0