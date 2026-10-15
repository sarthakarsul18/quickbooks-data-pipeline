import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from src.api import app
from src.config import settings


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    """Test /health endpoint returns healthy status and metadata."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "quickbooks" in data
    assert "bigquery" in data
    assert data["bigquery"]["project_id"] == "stone-column-511709-d4"
    assert data["bigquery"]["dataset"] == "quickbooks_data"


def test_auth_login_redirect(client, monkeypatch):
    """Test /auth/login redirects to QuickBooks authorization URL."""
    monkeypatch.setattr(settings, "QBO_CLIENT_ID", "test_id")
    monkeypatch.setattr(settings, "QBO_CLIENT_SECRET", "test_secret")

    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 307
    redirect_location = response.headers.get("location")
    assert "appcenter.intuit.com/connect/oauth2" in redirect_location
    assert "client_id=test_id" in redirect_location


def test_auth_callback_success(client):
    """Test /callback receives auth code, exchanges tokens, and returns success HTML."""
    mock_token_result = {
        "access_token": "sample_access",
        "refresh_token": "sample_refresh",
        "realm_id": "1234567890",
    }

    with patch("src.api.auth_manager.exchange_code_for_tokens", return_value=mock_token_result):
        response = client.get("/callback?code=test_code_123&realmId=1234567890&state=abc")
        assert response.status_code == 200
        assert "Authentication Successful!" in response.text
        assert "1234567890" in response.text


def test_auth_callback_error(client):
    """Test /callback handles OAuth error parameter gracefully."""
    response = client.get("/callback?error=access_denied")
    assert response.status_code == 400
    assert "QuickBooks Authorization Failed" in response.text


def test_status_endpoint(client, tmp_path):
    """Test /status returns sync state information."""
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "last_successful_sync" in data
    assert "supported_entities" in data
    assert len(data["supported_entities"]) == 8
