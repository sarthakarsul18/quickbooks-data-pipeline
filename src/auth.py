import base64
import json
import logging
import time
from typing import Any, Dict, Optional
import urllib.parse
import requests

from src.config import settings

logger = logging.getLogger(__name__)


class QuickBooksAuth:
    """Manages OAuth 2.0 flow, token storage, and token refreshing for QuickBooks Online."""

    def __init__(self, tokens_path=None):
        self.tokens_path = tokens_path or settings.TOKENS_FILE_PATH
        self._tokens: Dict[str, Any] = self._load_tokens()

    def get_authorization_url(self, state: str = "qbo_state_token") -> str:
        """Generates the URL to redirect the user to QuickBooks for OAuth authorization.

        Scope: com.intuit.quickbooks.accounting (standard accounting read/write scope)
        """
        params = {
            "client_id": settings.QBO_CLIENT_ID,
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "redirect_uri": settings.QBO_REDIRECT_URI,
            "state": state,
        }
        encoded_params = urllib.parse.urlencode(params)
        return f"{settings.qbo_auth_url}?{encoded_params}"

    def exchange_code_for_tokens(self, auth_code: str, realm_id: Optional[str] = None) -> Dict[str, Any]:
        """Exchanges authorization code received from callback for access and refresh tokens."""
        if not settings.QBO_CLIENT_ID or not settings.QBO_CLIENT_SECRET:
            raise ValueError("QBO_CLIENT_ID and QBO_CLIENT_SECRET must be configured.")

        # HTTP Basic Auth header: Base64(client_id:client_secret)
        credentials = f"{settings.QBO_CLIENT_ID}:{settings.QBO_CLIENT_SECRET}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {encoded_creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": settings.QBO_REDIRECT_URI,
        }

        logger.info("Exchanging authorization code for tokens with Intuit OAuth...")
        response = requests.post(settings.qbo_token_url, headers=headers, data=data, timeout=30)

        if response.status_code != 200:
            error_msg = f"Failed to exchange code: HTTP {response.status_code} - {response.text}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        token_data = response.json()
        # Calculate absolute expiration timestamp (seconds since epoch)
        # expires_in is typically 3600 seconds (1 hour)
        expires_in = token_data.get("expires_in", 3600)
        token_data["expires_at"] = int(time.time()) + expires_in
        if realm_id:
            token_data["realm_id"] = realm_id
        elif settings.QBO_REALM_ID:
            token_data["realm_id"] = settings.QBO_REALM_ID

        self._save_tokens(token_data)
        logger.info("Successfully acquired and stored new QuickBooks OAuth tokens.")
        return token_data

    def refresh_access_token(self) -> Dict[str, Any]:
        """Refreshes the access token using the stored refresh token."""
        refresh_token = self._tokens.get("refresh_token")
        if not refresh_token:
            raise ValueError("No refresh token available. Re-authorization via OAuth callback is required.")

        credentials = f"{settings.QBO_CLIENT_ID}:{settings.QBO_CLIENT_SECRET}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {encoded_creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        logger.info("Refreshing QuickBooks access token...")
        response = requests.post(settings.qbo_token_url, headers=headers, data=data, timeout=30)

        if response.status_code != 200:
            error_msg = f"Failed to refresh token: HTTP {response.status_code} - {response.text}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        token_data = response.json()
        expires_in = token_data.get("expires_in", 3600)
        token_data["expires_at"] = int(time.time()) + expires_in

        # Preserve realm_id if not returned in refresh response
        if "realm_id" not in token_data and "realm_id" in self._tokens:
            token_data["realm_id"] = self._tokens["realm_id"]

        self._save_tokens(token_data)
        logger.info("QuickBooks access token refreshed successfully.")
        return token_data

    def get_valid_access_token(self) -> str:
        """Returns a valid access token.

        Refreshes automatically if the current token is within 5 minutes of expiring.
        """
        if not self._tokens or "access_token" not in self._tokens:
            raise RuntimeError("Pipeline is not authenticated with QuickBooks. Please visit /auth/login first.")

        expires_at = self._tokens.get("expires_at", 0)
        # If expiring in less than 300 seconds (5 mins), refresh now
        if time.time() > (expires_at - 300):
            logger.info("Access token expired or close to expiration. Refreshing...")
            self.refresh_access_token()

        return self._tokens["access_token"]

    def get_realm_id(self) -> str:
        """Returns the active QuickBooks Company ID (realmId)."""
        realm_id = self._tokens.get("realm_id") or settings.QBO_REALM_ID
        if not realm_id:
            raise ValueError(
                "QuickBooks realm_id is missing. Please set QBO_REALM_ID in .env or authenticate via OAuth callback."
            )
        return realm_id

    def is_authenticated(self) -> bool:
        """Returns True if tokens exist and refresh token is present."""
        return bool(self._tokens.get("refresh_token"))

    def _load_tokens(self) -> Dict[str, Any]:
        """Loads token dictionary from the local file if it exists."""
        if self.tokens_path.exists():
            try:
                with open(self.tokens_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not read tokens file {self.tokens_path}: {e}")
        return {}

    def _save_tokens(self, tokens: Dict[str, Any]) -> None:
        """Saves token dictionary to the local file."""
        self._tokens = tokens
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.tokens_path, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2)


# Default auth manager instance
auth_manager = QuickBooksAuth()
