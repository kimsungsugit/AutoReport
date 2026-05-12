@echo off
REM AutoReport - Daily report generation (triggered by Windows Task Scheduler)
set PYTHONIOENCODING=utf-8
set PYTHONPATH=D:\Project\Program\AutoReport

cd /d "D:\Project\Program\AutoReport"
echo [%date% %time%] Starting report generation >> reports\scheduler.log 2>&1
"C:\msys64\mingw64\bin\python.exe" scripts\generate_multi_project_reports.py >> reports\scheduler.log 2>&1
set GEN_RC=%ERRORLEVEL%
echo [%date% %time%] Finished generator rc=%GEN_RC% >> reports\scheduler.log 2>&1

REM Start proxy if not running, then open dashboard
REM `start ""` fails under the non-interactive Task Scheduler session and previously
REM leaked exit code 0x2B; wrap it so post-generation failures don't mask success.
"C:\msys64\mingw64\bin\python.exe" -c "import urllib.request; urllib.request.urlopen('http://localhost:18923/', timeout=2)" 2>nul
if errorlevel 1 (
    start "" /b "C:\msys64\mingw64\bin\python.exe" scripts\jira_proxy.py
    timeout /t 3 /nobreak >nul
)
start "" "http://localhost:18923/portfolio" 2>nul
exit /b %GEN_RC%
