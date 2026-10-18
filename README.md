# QuickBooks Online to BigQuery Data Pipeline

A Python pipeline that extracts accounting data from the QuickBooks Online (QBO) Sandbox and loads it into Google BigQuery. It supports full and incremental syncs, soft-delete tracking, logs, and Windows Task Scheduler execution.

## Entities

- Accounts
- Customers
- Vendors
- Invoices
- Payments
- Bills
- Bill Payments
- Journal Entries

## Project files

| File/folder | Purpose |
|---|---|
| `src/sync.py` | Main sync workflow |
| `src/scheduler.py` | In-process scheduler; runs immediately and repeats every four hours while the process stays alive |
| `src/api.py` | FastAPI health/status and on-demand sync endpoints |
| `.env.example` | Configuration template |
| `.env` | Local secrets/configuration; never commit |
| `.tokens.json` | Local QBO OAuth tokens; never commit |
| `sync_state.json` | Incremental sync checkpoint |
| `run_scheduled_sync.bat` | One-shot sync script launched by Windows Task Scheduler |
| `evidence/` | Logs and assignment evidence |
| `ai-chats/` | Relevant AI conversation links/exports |
| `tests/` | Automated tests |

## Prerequisites

- Python 3.11 or compatible
- Intuit Developer account and QBO Sandbox company
- Google Cloud project with BigQuery enabled and dataset permissions
- Google Cloud CLI (`gcloud`)
- Git
- Windows for the Task Scheduler instructions below

## Local setup

Run commands from the repository root.

### 1. Create and activate the virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

If activation is not needed, call the environment's Python directly using `.\venv\Scripts\python.exe`.

### 2. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Edit `.env` with the values expected by `.env.example`, including the Intuit app client ID/secret, redirect URI, `QBO_ENVIRONMENT=sandbox`, Google Cloud project ID, and BigQuery dataset. The redirect URI must match the one configured in the Intuit Developer app. Never commit `.env`.

### 4. Authenticate with Google Cloud

```powershell
gcloud auth application-default login
```

Sign in with a Google account that has access to the target BigQuery project and dataset.

### 5. Authorize QuickBooks Sandbox

Start the API:

```powershell
.\venv\Scripts\python.exe -m uvicorn src.api:app --reload --port 8000
```

Open `http://localhost:8000/auth/login`, sign in to the Sandbox company, and complete the OAuth flow. Tokens are stored locally in `.tokens.json`. Check `http://localhost:8000/health` if supported by the current app. Stop the server with `Ctrl+C` when done.

## Project configuration

Current assignment configuration:

- Google Cloud project: `stone-column-511709-d4`
- BigQuery dataset: `quickbooks_data`
- QuickBooks Sandbox company: Bluepeak Cloud Solutions Inc.

These are project-specific values. Use your own authorized configuration if running elsewhere.

## Run the pipeline

Use the virtual environment's Python to ensure the expected dependencies are used.

### Dry run

```powershell
.\venv\Scripts\python.exe -m src.sync --dry-run
```

### Full sync

```powershell
.\venv\Scripts\python.exe -m src.sync --full
```

### Incremental sync

```powershell
.\venv\Scripts\python.exe -m src.sync
```

A successful incremental run may report zero changed records if nothing changed since the saved checkpoint. Check the final sync status and exit code, not the record count alone.

### In-process four-hour scheduler

```powershell
.\venv\Scripts\python.exe -m src.scheduler
```

This keeps scheduling inside the running Python process. Closing the terminal or shutting down the computer stops it. For Windows scheduled runs that do not require this terminal to remain open, use Task Scheduler below.

### FastAPI service

```powershell
.\venv\Scripts\python.exe -m uvicorn src.api:app --reload --port 8000
```

Documented endpoints:
- `GET /health` — health/authentication status
- `GET /status` — last sync timestamp and statistics
- `POST /sync` — request an on-demand sync

Check `src/api.py` for the current implementation before exposing it outside localhost.

## Incremental sync and deletion handling

The pipeline uses `sync_state.json` to track the last successful sync. It uses QuickBooks Change Data Capture (CDC) to fetch changed records. Depending on the checkpoint age or CDC availability, the implementation can fall back to querying by `MetaData.LastUpdatedTime` with an overlap buffer. Records are deduplicated by ID and written to BigQuery. The checkpoint should update only after a successful sync.

The pipeline retains deleted records in BigQuery using a tombstone/`is_deleted` flag rather than necessarily removing the row. For validation, inspect the record ID, status, and `is_deleted`. Inactive master records and deleted transactions are different cases.

In BigQuery Sandbox, some DML operations may be unsupported. The code may use its implemented CTAS/table-rebuild fallback; check the final sync status and query the table to verify the result.

## Windows Task Scheduler: unattended sync every four hours

This method uses **Task Scheduler + `run_scheduled_sync.bat`**. It launches a separate one-shot incremental sync on each trigger. The computer must be powered on and Windows must be running at the scheduled time.

### 1. Create `run_scheduled_sync.bat`

Create this file in the repository root. Change the path if the project is located elsewhere.

```bat
@echo off
cd /d "C:\Users\Care\Desktop\quickbooks-data-pipeline"
if not exist evidence mkdir evidence
echo ==== Started %date% %time% ==== >> evidence\scheduled_sync.log
venv\Scripts\python.exe -m src.sync >> evidence\scheduled_sync.log 2>&1
echo Exit code: %ERRORLEVEL% >> evidence\scheduled_sync.log
echo ==== Finished %date% %time% ==== >> evidence\scheduled_sync.log
```

What the batch file does:
1. Changes the working directory to the project root.
2. Creates `evidence` if it does not exist.
3. Appends a start timestamp to `evidence\scheduled_sync.log`.
4. Runs one incremental sync using the project's virtual-environment Python.
5. Appends the command output and errors to the same log.
6. Records the exit code and finish timestamp.

The batch file does **not** create the repeating schedule; Task Scheduler does that.

### 2. Configure Task Scheduler

1. Open **Task Scheduler**.
2. Select **Create Task**.
3. On **General**, name it `QuickBooks BigQuery Sync`. Use a Windows account that can access the project folder, `.tokens.json`, and Google ADC credentials.
4. On **Triggers**, create an enabled trigger and set it to repeat every **4 hours**. Choose the intended start time/date.
5. On **Actions**, add:
   - **Program/script:** `C:\Windows\System32\cmd.exe`
   - **Add arguments:** `/c "C:\Users\Care\Desktop\quickbooks-data-pipeline\run_scheduled_sync.bat"`
   - **Start in:** `C:\Users\Care\Desktop\quickbooks-data-pipeline`
6. On **Conditions**, review power conditions. If you expect runs on battery, disable the AC-power-only restriction as appropriate.
7. On **Settings**, enable **Run task as soon as possible after a scheduled start is missed**, if available and appropriate.
8. Save the task and provide credentials if Windows asks.

The example paths above match this machine's project location. Update all path references if the repository is moved.

### 3. Test and verify

You can manually test the task by selecting it in Task Scheduler and clicking **Run**. That is only a manual test; to prove unattended execution, verify a later scheduled run that occurred without clicking **Run**.

Inspect task status:

```powershell
Get-ScheduledTask -TaskName "QuickBooks BigQuery Sync"
Get-ScheduledTaskInfo -TaskName "QuickBooks BigQuery Sync" |
    Format-List LastRunTime,LastTaskResult,NextRunTime
```

`LastTaskResult : 0` usually means the task action completed successfully. Also inspect the application log because Task Scheduler success alone does not prove the sync succeeded.

Show the latest log entries:

```powershell
Get-Content .\evidence\scheduled_sync.log -Tail 35
```

Look for the latest `Started` timestamp, a successful final sync status, `Exit code: 0`, and the matching `Finished` timestamp. A successful run can process zero records if no data changed.

### 4. Append status evidence without overwriting old entries

Use `-Append` to retain earlier status snapshots:

```powershell
"===== Scheduled Task Status: $(Get-Date) =====" |
    Out-File evidence\scheduled_task_status.txt -Append

Get-ScheduledTaskInfo -TaskName "QuickBooks BigQuery Sync" |
    Format-List LastRunTime,LastTaskResult,NextRunTime |
    Out-File evidence\scheduled_task_status.txt -Append
```

Without `-Append`, `Out-File evidence\scheduled_task_status.txt` overwrites the existing file.

### 5. Shutdown and missed-run behavior

- A fully shut-down computer cannot run the task at the scheduled time.
- If Windows is running at the scheduled time, Task Scheduler can launch it.
- If a run was missed while the computer was off, the “run as soon as possible after a scheduled start is missed” setting may run it after startup. Verify the actual behavior in the log.
- Test the task under the exact Windows account and login conditions you intend to use, because Google ADC and OAuth tokens may be user-specific.

## BigQuery validation

Run the following query to compare the non-deleted record counts for all eight entity types in BigQuery:

```sql
SELECT 'Accounts' AS entity, COUNTIF(is_deleted IS NOT TRUE) AS record_count
FROM `stone-column-511709-d4.quickbooks_data.accounts`

UNION ALL

SELECT 'Customers', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.customers`

UNION ALL

SELECT 'Vendors', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.vendors`

UNION ALL

SELECT 'Invoices', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.invoices`

UNION ALL

SELECT 'Payments', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.payments`

UNION ALL

SELECT 'Bills', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.bills`

UNION ALL

SELECT 'Bill Payments', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.bill_payments`

UNION ALL

SELECT 'Journal Entries', COUNTIF(is_deleted IS NOT TRUE)
FROM `stone-column-511709-d4.quickbooks_data.journal_entries`;
```

## Test invoice lifecycle evidence

For the assignment's lifecycle test, use only an invoice created specifically for this test in the Sandbox company.

1. Create an invoice dated **September 1, 2026 or later**.
2. Set the memo to exactly `TEST – Sarthak Arsul`.
3. Run incremental sync and save the QuickBooks and BigQuery evidence for the created state.
4. Edit that same invoice, save it, run incremental sync, and save evidence for the updated state.
5. Delete that same test invoice, run incremental sync, and save evidence of its BigQuery tombstone/deleted state.
6. Keep the matching sync logs, screenshots, and SQL results in `evidence/`.

Do not delete shared/pre-existing records. If the lifecycle has already been performed, verify and organize the existing proof rather than creating extra test records.

## Evidence and AI conversations

Keep relevant proof in `evidence/`, such as:
- Full-sync log
- Incremental-sync log
- `scheduled_sync.log`
- `scheduled_task_status.txt` with appended snapshots
- BigQuery-vs-QuickBooks counts for all eight entities
- Invoice create/edit/delete screenshots and BigQuery query results

Keep links or exported conversations that materially assisted the assignment in `ai-chats/`. Do not fabricate transcripts. Confirm that shared links are accessible to the reviewer and remove any secrets from exports.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest -v
```

Only report tests as passing if the command completes successfully. The tests are intended to cover mocked API responses, OAuth handling, pagination/CDC parsing, BigQuery operations, checkpoint handling, failure behavior, and API endpoints.

## Git and secret safety

Before staging changes:

```powershell
git status --short
git diff --check
git diff --stat
```

Never commit `.env`, `.tokens.json`, Google credentials/service-account keys, or files containing access tokens, refresh tokens, or client secrets. Review `.gitignore` and the contents of every evidence file before staging.

Stage only intended files, for example:

```powershell
git add README.md ai-chats/ evidence/
git diff --cached --stat
git status --short
```

If the staged files are safe and correct:

```powershell
git commit -m "docs: document sync and scheduled execution"
git push
```

Do not blindly run `git add .` without reviewing the status first.

## Troubleshooting

**Task Scheduler ran, but the log did not update**
- Verify the action points to the correct batch file.
- Verify **Start in** is the repository root.
- Verify the task account can access the project, local token file, and Google ADC credentials.

**Task Scheduler shows success, but the sync failed**
- Inspect the latest log for exceptions, API errors, final status, and exit code.

**Incremental sync processed zero records**
- This can be normal when no records changed since the checkpoint. Verify final status and check for changes made after the checkpoint.

**Counts differ between QuickBooks and BigQuery**
- Confirm company, entity, date range, and UI filters match. Check inactive records and deletion/tombstone behavior.
