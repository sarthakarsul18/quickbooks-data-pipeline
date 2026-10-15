"""Unit tests for QuickBooks Online OAuth 2.0 authentication module."""
import json
import time
import pytest
from unittest.mock import MagicMock, patch

from src.auth import QuickBooksAuth
from src.config import settings


def test_get_authorization_url(monkeypatch):
    """Test generating Intuit OAuth2 authorization URL."""
    monkeypatch.setattr(settings, "QBO_CLIENT_ID", "test_client_id")
    monkeypatch.setattr(settings, "QBO_REDIRECT_URI", "http://localhost:8000/callback")

    auth = QuickBooksAuth(tokens_path=MagicMock())
    url = auth.get_authorization_url(state="xyz123")

    assert "https://appcenter.intuit.com/connect/oauth2" in url
    assert "client_id=test_client_id" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fcallback" in url
    assert "state=xyz123" in url
    assert "scope=com.intuit.quickbooks.accounting" in url


def test_exchange_code_for_tokens(tmp_path, monkeypatch, mock_token_response):
    """Test exchanging authorization code for access and refresh tokens."""
    monkeypatch.setattr(settings, "QBO_CLIENT_ID", "test_id")
    monkeypatch.setattr(settings, "QBO_CLIENT_SECRET", "test_secret")

    tokens_file = tmp_path / ".tokens.json"
    auth = QuickBooksAuth(tokens_path=tokens_file)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_token_response

    with patch("requests.post", return_value=mock_resp) as mock_post:
        result = auth.exchange_code_for_tokens(auth_code="sample_code", realm_id="123456789")

        assert result["access_token"] == "mock_access_token_12345"
        assert result["refresh_token"] == "mock_refresh_token_67890"
        assert result["realm_id"] == "123456789"
        assert "expires_at" in result

        # Verify tokens saved to disk
        assert tokens_file.exists()
        saved = json.loads(tokens_file.read_text(encoding="utf-8"))
        assert saved["access_token"] == "mock_access_token_12345"

        mock_post.assert_called_once()


def test_refresh_access_token(tmp_path, monkeypatch, mock_token_response):
    """Test refreshing an expired access token."""
    monkeypatch.setattr(settings, "QBO_CLIENT_ID", "test_id")
    monkeypatch.setattr(settings, "QBO_CLIENT_SECRET", "test_secret")

    tokens_file = tmp_path / ".tokens.json"
    initial_tokens = {
        "access_token": "old_token",
        "refresh_token": "old_refresh",
        "realm_id": "987654321",
        "expires_at": int(time.time()) - 100,  # Expired
    }
    tokens_file.write_text(json.dumps(initial_tokens), encoding="utf-8")

    auth = QuickBooksAuth(tokens_path=tokens_file)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    new_token_data = {
        "access_token": "refreshed_access_token",
        "refresh_token": "new_refresh_token",
        "expires_in": 3600,
    }
    mock_resp.json.return_value = new_token_data

    with patch("requests.post", return_value=mock_resp):
        refreshed = auth.refresh_access_token()
        assert refreshed["access_token"] == "refreshed_access_token"
        assert refreshed["realm_id"] == "987654321"

        # Verify valid token method returns new token
        assert auth.get_valid_access_token() == "refreshed_access_token"


def test_get_valid_access_token_not_expired(tmp_path):
    """Test getting token without refresh when token is still valid."""
    tokens_file = tmp_path / ".tokens.json"
    valid_tokens = {
        "access_token": "valid_active_token",
        "refresh_token": "sample_refresh",
        "expires_at": int(time.time()) + 1800,  # 30 mins remaining
    }
    tokens_file.write_text(json.dumps(valid_tokens), encoding="utf-8")

    auth = QuickBooksAuth(tokens_path=tokens_file)
    with patch("requests.post") as mock_post:
        token = auth.get_valid_access_token()
        assert token == "valid_active_token"
        mock_post.assert_not_called()
