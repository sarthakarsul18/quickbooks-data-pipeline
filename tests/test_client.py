"""Unit tests for QuickBooks Online API client."""
import pytest
from unittest.mock import MagicMock, patch

from src.auth import QuickBooksAuth
from src.client import QuickBooksClient
from src.extractor import SUPPORTED_ENTITIES


@pytest.fixture
def mock_auth():
    """Mock auth provider."""
    auth = MagicMock(spec=QuickBooksAuth)
    auth.get_valid_access_token.return_value = "mock_token"
    auth.get_realm_id.return_value = "123456789"
    return auth


def test_query_pagination_uses_post(mock_auth, mock_query_response):
    """Test that query uses HTTP POST with plain-text body and minorversion=75."""
    client = QuickBooksClient(auth=mock_auth)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_query_response

    with patch("requests.post", return_value=mock_resp) as mock_post:
        records = list(client.query("Invoice"))

        assert len(records) == 2
        assert mock_post.call_count == 1

        # Verify POST method arguments
        call_args, call_kwargs = mock_post.call_args
        url = call_args[0]
        assert "/v3/company/123456789/query" in url
        assert call_kwargs["params"]["minorversion"] == "75"
        assert call_kwargs["headers"]["Content-Type"] == "application/text"
        assert call_kwargs["headers"]["Accept"] == "application/json"
        assert call_kwargs["headers"]["Authorization"] == "Bearer mock_token"
        assert "SELECT * FROM Invoice STARTPOSITION 1 MAXRESULTS 1000" in call_kwargs["data"]


def test_query_all_eight_entities(mock_auth):
    """Verify that all 8 required accounting entities can be queried cleanly."""
    client = QuickBooksClient(auth=mock_auth)

    expected_entities = [
        "Account",
        "Customer",
        "Vendor",
        "Invoice",
        "Payment",
        "Bill",
        "BillPayment",
        "JournalEntry",
    ]

    for entity in expected_entities:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"QueryResponse": {entity: [{"Id": f"{entity}_1"}]}}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            records = list(client.query(entity))
            assert len(records) == 1
            assert records[0]["Id"] == f"{entity}_1"
            assert f"SELECT * FROM {entity}" in mock_post.call_args[1]["data"]


def test_get_cdc_uses_get_with_minorversion(mock_auth, mock_cdc_response):
    """Test CDC endpoint querying uses GET with minorversion=75 and parses response."""
    client = QuickBooksClient(auth=mock_auth)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_cdc_response

    with patch("requests.get", return_value=mock_resp) as mock_get:
        results = client.get_cdc(
            entities=["Invoice", "Customer", "Account"],
            changed_since="2026-10-15T00:00:00Z",
        )

        assert mock_get.call_count == 1
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        headers = mock_get.call_args[1]["headers"]

        assert "/v3/company/123456789/cdc" in url
        assert params["entities"] == "Invoice,Customer,Account"
        assert params["changedSince"] == "2026-10-15T00:00:00Z"
        assert params["minorversion"] == "75"
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"

        assert len(results["Invoice"]) == 2
        assert len(results["Customer"]) == 1
        assert len(results["Account"]) == 0

        # Check that deleted invoice is captured
        assert results["Invoice"][1]["status"] == "Deleted"
        assert results["Invoice"][1]["Id"] == "999"


def test_client_retry_on_401(mock_auth):
    """Test that client refreshes token and retries when receiving 401 Unauthorized."""
    client = QuickBooksClient(auth=mock_auth)

    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = {"QueryResponse": {"Account": [{"Id": "1"}]}}

    with patch("requests.post", side_effect=[mock_resp_401, mock_resp_200]):
        records = list(client.query("Account"))
        assert len(records) == 1
        assert records[0]["Id"] == "1"
        mock_auth.refresh_access_token.assert_called_once()


def test_safety_guard_blocks_mutating_endpoints(mock_auth):
    """Test that client blocks any attempt to call mutating endpoints."""
    client = QuickBooksClient(auth=mock_auth)

    with pytest.raises(ValueError, match="Safety Violation"):
        client._request_with_retry("invoice", method="POST", data='{"TotalAmt": 100}')

    with pytest.raises(ValueError, match="Safety Violation"):
        client._request_with_retry("bill/123", method="DELETE")


def test_fault_payload_raises_runtime_error_on_query(mock_auth):
    """Test that QuickBooks API Fault response raises RuntimeError even if HTTP status is 200."""
    client = QuickBooksClient(auth=mock_auth)

    mock_resp_fault = MagicMock()
    mock_resp_fault.status_code = 200
    mock_resp_fault.json.return_value = {
        "Fault": {
            "Error": [
                {"Message": "Invalid query syntax", "Detail": "Query parsing failed", "code": "4000"}
            ],
            "type": "ValidationFault",
        }
    }

    with patch("requests.post", return_value=mock_resp_fault):
        with pytest.raises(RuntimeError, match="QuickBooks API Fault received on query"):
            list(client.query("Bill"))


def test_fault_payload_raises_runtime_error_on_cdc(mock_auth):
    """Test that QuickBooks CDC Fault response raises RuntimeError even if HTTP status is 200."""
    client = QuickBooksClient(auth=mock_auth)

    mock_resp_fault = MagicMock()
    mock_resp_fault.status_code = 200
    mock_resp_fault.json.return_value = {
        "Fault": {
            "Error": [
                {"Message": "Invalid changedSince timestamp", "Detail": "Must be within 30 days", "code": "5000"}
            ],
            "type": "ValidationFault",
        }
    }

    with patch("requests.get", return_value=mock_resp_fault):
        with pytest.raises(RuntimeError, match="QuickBooks API Fault received on cdc"):
            client.get_cdc(["Invoice"], "2026-10-15T00:00:00Z")
