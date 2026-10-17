@echo off
cd /d "C:\Users\Care\Desktop\quickbooks-data-pipeline"
if not exist evidence mkdir evidence
echo ==== Started %date% %time% ==== >> evidence\scheduled_sync.log
venv\Scripts\python.exe -m src.sync >> evidence\scheduled_sync.log 2>&1
echo Exit code: %ERRORLEVEL% >> evidence\scheduled_sync.log
echo ==== Finished %date% %time% ==== >> evidence\scheduled_sync.log
