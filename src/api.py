import datetime
import logging
from typing import Any, Dict, Optional
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from src.auth import auth_manager
from src.config import settings
from src.extractor import QuickBooksExtractor

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [API] %(message)s",
)
logger = logging.getLogger("src.api")

app = FastAPI(
    title="QuickBooks Online Data Pipeline",
    description="OAuth 2.0 handler, health checks, and sync management for QBO to BigQuery",
    version="1.0.0",
)


@app.get("/health", summary="Health check endpoint")
def health() -> Dict[str, Any]:
    """Returns application health and QuickBooks connection status."""
    is_authenticated = auth_manager.is_authenticated()
    realm_id = None
    try:
        if is_authenticated:
            realm_id = auth_manager.get_realm_id()
    except Exception:
        pass

    return {
        "status": "healthy",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "quickbooks": {
            "authenticated": is_authenticated,
            "environment": settings.QBO_ENVIRONMENT,
            "realm_id": realm_id,
        },
        "bigquery": {
            "project_id": settings.GCP_PROJECT_ID,
            "dataset": settings.BIGQUERY_DATASET,
            "location": settings.BIGQUERY_LOCATION,
        },
    }


@app.get("/auth/login", summary="Initiate QuickBooks OAuth2 authorization")
def auth_login():
    """Redirects the browser to Intuit Developer OAuth 2.0 consent page."""
    if not settings.QBO_CLIENT_ID or not settings.QBO_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="QuickBooks credentials (QBO_CLIENT_ID and QBO_CLIENT_SECRET) are not configured in .env",
        )

    auth_url = auth_manager.get_authorization_url()
    logger.info(f"Redirecting user to QuickBooks OAuth: {auth_url}")
    return RedirectResponse(url=auth_url)


@app.get("/callback", summary="QuickBooks OAuth2 callback endpoint")
def auth_callback(
    code: Optional[str] = Query(None, description="QuickBooks authorization code"),
    realmId: Optional[str] = Query(None, description="QuickBooks sandbox company ID"),
    state: Optional[str] = Query(None, description="OAuth state parameter"),
    error: Optional[str] = Query(None, description="OAuth error code if any"),
):
    """Processes the callback from Intuit OAuth, exchanges the code for tokens, and persists them."""
    if error:
        logger.error(f"QuickBooks OAuth error received: {error}")
        return HTMLResponse(
            content=f"<h2>QuickBooks Authorization Failed</h2><p>Error: {error}</p>",
            status_code=400,
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing required 'code' parameter in OAuth callback.")

    try:
        tokens = auth_manager.exchange_code_for_tokens(auth_code=code, realm_id=realmId)
        company_id = tokens.get("realm_id", realmId or "Unknown")

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>QuickBooks Online Authentication Successful</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                       display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background-color: #f7fafc; }}
                .card {{ background: white; padding: 2.5rem; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); max-width: 500px; }}
                h1 {{ color: #2b6cb0; margin-top: 0; }}
                code {{ background: #edf2f7; padding: 2px 6px; border-radius: 4px; }}
                .btn {{ display: inline-block; margin-top: 1rem; padding: 0.5rem 1rem; background: #3182ce; color: white; text-decoration: none; border-radius: 4px; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Authentication Successful!</h1>
                <p>QuickBooks Online Sandbox is connected.</p>
                <p><strong>Realm ID (Company ID):</strong> <code>{company_id}</code></p>
                <p>Tokens have been saved securely. You can now execute pipeline extractions:</p>
                <ul>
                    <li>Run manual sync: <code>python -m src.sync</code></li>
                    <li>Run scheduler (4h): <code>python -m src.scheduler</code></li>
                </ul>
                <a class="btn" href="/health">Check Health Status</a>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content)

    except Exception as e:
        logger.exception(f"Error handling OAuth callback: {e}")
        return HTMLResponse(
            content=f"<h2>Authentication Error</h2><p>{str(e)}</p>",
            status_code=500,
        )


@app.get("/status", summary="Get pipeline sync status")
def pipeline_status():
    """Returns the last recorded sync time and configuration."""
    extractor = QuickBooksExtractor()
    last_sync = extractor.load_last_sync_time()

    return {
        "last_successful_sync": last_sync.isoformat() if last_sync else None,
        "is_authenticated": auth_manager.is_authenticated(),
        "supported_entities": [
            "Account",
            "Customer",
            "Vendor",
            "Invoice",
            "Payment",
            "Bill",
            "BillPayment",
            "JournalEntry",
        ],
    }


def _run_sync_task(full: bool):
    """Background task runner for on-demand sync."""
    try:
        extractor = QuickBooksExtractor()
        extractor.sync(force_full=full)
    except Exception as e:
        logger.exception(f"Background sync failed: {e}")


@app.post("/sync", summary="Trigger manual sync via API")
def trigger_sync(background_tasks: BackgroundTasks, full: bool = False):
    """Triggers an asynchronous extraction job in the background."""
    if not auth_manager.is_authenticated():
        raise HTTPException(
            status_code=401,
            detail="Pipeline is not authenticated. Please visit /auth/login first.",
        )

    background_tasks.add_task(_run_sync_task, full=full)
    return {
        "status": "triggered",
        "mode": "full" if full else "incremental",
        "message": "Sync extraction task has been queued in background.",
    }
