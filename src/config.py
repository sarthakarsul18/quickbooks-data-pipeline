import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    # QuickBooks Online OAuth Credentials
    QBO_CLIENT_ID: str = Field(default="", description="QuickBooks App Client ID")
    QBO_CLIENT_SECRET: str = Field(default="", description="QuickBooks App Client Secret")
    QBO_REDIRECT_URI: str = Field(
        default="http://localhost:8000/callback",
        description="OAuth 2.0 Redirect URI registered in Intuit Developer Portal",
    )
    QBO_ENVIRONMENT: str = Field(
        default="sandbox",
        description="QuickBooks environment ('sandbox' or 'production')",
    )
    QBO_REALM_ID: str = Field(
        default="",
        description="QuickBooks Company ID (realmId)",
    )
    QBO_MINOR_VERSION: str = Field(
        default="75",
        description="QuickBooks API minor version parameter",
    )

    # Google Cloud & BigQuery Configuration
    GCP_PROJECT_ID: str = Field(
        default="stone-column-511709-d4",
        description="Google Cloud Project ID",
    )
    BIGQUERY_DATASET: str = Field(
        default="quickbooks_data",
        description="BigQuery dataset name",
    )
    BIGQUERY_LOCATION: str = Field(
        default="US",
        description="BigQuery dataset regional location",
    )

    # Application & Scheduler Configuration
    API_HOST: str = Field(default="0.0.0.0", description="FastAPI host")
    API_PORT: int = Field(default=8000, description="FastAPI port")
    SYNC_INTERVAL_HOURS: int = Field(
        default=4,
        description="Interval in hours for the automated sync scheduler",
    )
    LOG_LEVEL: str = Field(default="INFO", description="Application logging level")

    # Local storage paths for token & sync state
    TOKENS_FILE_PATH: Path = BASE_DIR / ".tokens.json"
    STATE_FILE_PATH: Path = BASE_DIR / "sync_state.json"

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def qbo_base_url(self) -> str:
        """Returns the base API URL depending on the configured environment."""
        if self.QBO_ENVIRONMENT.lower() == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"

    @property
    def qbo_auth_url(self) -> str:
        """Intuit OAuth2 authorization URL."""
        return "https://appcenter.intuit.com/connect/oauth2"

    @property
    def qbo_token_url(self) -> str:
        """Intuit OAuth2 token exchange endpoint."""
        return "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


# Global settings singleton
settings = Settings()
