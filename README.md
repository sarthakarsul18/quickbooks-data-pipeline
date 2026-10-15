# QuickBooks Online to BigQuery Data Pipeline

## Project Overview

This project is a Python data pipeline that extracts accounting data from the QuickBooks Online Sandbox API and loads it into Google BigQuery.

It extracts eight entities:
- Accounts
- Customers
- Vendors
- Invoices
- Payments
- Bills
- Bill Payments
- Journal Entries

The pipeline supports initial full syncs, incremental syncs, deleted record tracking with soft deletes, and scheduled runs every four hours.

## Requirements

- Python 3.11 or higher
- Intuit Developer account with a QuickBooks Online sandbox company
- Google Cloud project with BigQuery enabled
- Google Cloud CLI (`gcloud`) installed for local authentication

## Setup

1. **Create and activate a virtual environment**:
   ```bash
   python -m venv venv
   # On Windows:
   .\venv\Scripts\activate
   # On Linux / macOS:
   source venv/bin/activate
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**:
   Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
   Fill in your QuickBooks credentials and BigQuery details in `.env`:
   - `QBO_CLIENT_ID` and `QBO_CLIENT_SECRET`: From your Intuit Developer App keys.
   - `QBO_REDIRECT_URI`: Set to `http://localhost:8000/callback`.
   - `QBO_ENVIRONMENT`: Set to `sandbox`.
   - `GCP_PROJECT_ID`: Your Google Cloud project ID (e.g. `stone-column-511709-d4`).
   - `BIGQUERY_DATASET`: Target dataset name (default: `quickbooks_data`).

4. **Authenticate BigQuery**:
   Log in with Application Default Credentials (no service account JSON key file required):
   ```bash
   gcloud auth application-default login
   ```

5. **Authenticate QuickBooks Online**:
   Start the local FastAPI server:
   ```bash
   uvicorn src.api:app --reload --port 8000
   ```
   Open `http://localhost:8000/auth/login` in your browser and sign in with your Intuit sandbox credentials. Once authorized, tokens are stored in `.tokens.json`. You can verify connection status at `http://localhost:8000/health`.

## How to Run

- **Manual Incremental Sync** (runs a full sync on the first run, then incremental on subsequent runs):
  ```bash
  python -m src.sync
  ```

- **Force a Full Sync**:
  ```bash
  python -m src.sync --full
  ```

- **Dry Run** (verifies QuickBooks and BigQuery connectivity without writing data):
  ```bash
  python -m src.sync --dry-run
  ```

- **Automated Scheduler** (runs immediately and then repeats every 4 hours while the terminal remains open):
  ```bash
  python -m src.scheduler
  ```

- **FastAPI Endpoints** (start server with `uvicorn src.api:app --port 8000`):
  - `GET /health`: Health and authentication status.
  - `GET /status`: Last sync timestamp and stats.
  - `POST /sync`: Triggers an on-demand sync run.

## Incremental Sync

The pipeline tracks sync progress using a local file named `sync_state.json`.

- When you run a sync, it reads `last_successful_sync` from `sync_state.json`.
- It calls QuickBooks Change Data Capture (CDC) API (`GET /v3/company/{realm_id}/cdc`) using `changedSince` set to the last sync time to fetch created, updated, and deleted records.
- If CDC fails or if the last sync was more than 29.5 days ago (CDC has a 30-day limit), it falls back to querying entities using `MetaData.LastUpdatedTime` with a 60-second overlap buffer.
- Records are deduplicated by record `id` and upserted into BigQuery so that existing records are updated without duplicates.
- The checkpoint in `sync_state.json` is updated only after all entities are extracted and loaded successfully.

## Deleted Records

QuickBooks records are never physically deleted from BigQuery. Instead, they are tracked using a boolean tombstone flag:

- BigQuery tables include an `is_deleted` column.
- For transactions (Invoices, Payments, Bills, Bill Payments, Journal Entries), CDC returns deleted items with `status="Deleted"`.
- For master records (Accounts, Customers, Vendors), inactive items have `Active=false`.
- If either condition is met, the pipeline marks `is_deleted = TRUE` in the target BigQuery table while keeping the historic record and its payload.

## Testing

Run the test suite with:

```bash
pytest -v
```

The tests use mocked QuickBooks API responses and mocked BigQuery operations. They verify:
- OAuth URL generation, token exchange, and refresh handling.
- Read-only protection preventing mutation endpoints.
- Query pagination using POST and CDC response parsing.
- BigQuery table creation, deduplication, and Sandbox fallback logic.
- Checkpoint persistence and failure handling.
- FastAPI health and callback endpoints.
