import json
import pytest
from unittest.mock import MagicMock
from google.cloud import bigquery


@pytest.fixture
def mock_token_response():
    """Mocked OAuth2 token response from Intuit."""
    return {
        "access_token": "mock_access_token_12345",
        "refresh_token": "mock_refresh_token_67890",
        "token_type": "bearer",
        "expires_in": 3600,
        "x_refresh_token_expires_in": 8726400,
    }


@pytest.fixture
def mock_query_response():
    """Mocked QuickBooks SELECT query response for Invoices."""
    return {
        "QueryResponse": {
            "Invoice": [
                {
                    "Id": "101",
                    "DocNumber": "INV-1001",
                    "TotalAmt": 250.75,
                    "MetaData": {
                        "CreateTime": "2026-10-01T12:00:00Z",
                        "LastUpdatedTime": "2026-10-10T15:30:00Z",
                    },
                    "CustomerRef": {"value": "1", "name": "Amy's Bird Sanctuary"},
                },
                {
                    "Id": "102",
                    "DocNumber": "INV-1002",
                    "TotalAmt": 500.00,
                    "MetaData": {
                        "CreateTime": "2026-10-02T12:00:00Z",
                        "LastUpdatedTime": "2026-10-11T16:00:00Z",
                    },
                    "CustomerRef": {"value": "2", "name": "Bill's Windsurf Shop"},
                },
            ],
            "startPosition": 1,
            "maxResults": 2,
        }
    }


@pytest.fixture
def mock_cdc_response():
    """Mocked QuickBooks Change Data Capture (CDC) response.

    Contains both an updated Invoice and a deleted Invoice with status="Deleted".
    """
    return {
        "CDCResponse": [
            {
                "QueryResponse": [
                    {
                        "Invoice": [
                            {
                                "Id": "101",
                                "DocNumber": "INV-1001-EDITED",
                                "TotalAmt": 300.00,
                                "MetaData": {
                                    "LastUpdatedTime": "2026-10-15T09:00:00Z",
                                },
                            },
                            {
                                "Id": "999",
                                "status": "Deleted",
                                "MetaData": {
                                    "LastUpdatedTime": "2026-10-15T09:30:00Z",
                                },
                            },
                        ],
                        "Customer": [
                            {
                                "Id": "1",
                                "DisplayName": "Amy's Bird Sanctuary Inactive",
                                "Active": False,
                                "MetaData": {
                                    "LastUpdatedTime": "2026-10-15T08:00:00Z",
                                },
                            }
                        ],
                    }
                ]
            }
        ]
    }


@pytest.fixture
def mock_bigquery_client():
    """Mocked Google BigQuery Client."""
    client = MagicMock(spec=bigquery.Client)
    client.project = "stone-column-511709-d4"
    client.location = "US"

    # Mock dataset ref
    dataset_ref = MagicMock()
    client.dataset.return_value = dataset_ref

    # Mock load job
    load_job = MagicMock()
    load_job.result.return_value = None
    client.load_table_from_json.return_value = load_job

    # Mock query job
    query_job = MagicMock()
    query_job.result.return_value = None
    client.query.return_value = query_job

    return client
