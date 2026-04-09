@echo off
REM AutoReport - Daily report generation (triggered by Windows Task Scheduler)
set PYTHONIOENCODING=utf-8
set PYTHONPATH=D:\Project\Program\AutoReport

cd /d "D:\Project\Program\AutoReport"
echo [%date% %time%] Starting report generation >> reports\scheduler.log 2>&1
"C:\msys64\mingw64\bin\python.exe" scripts\generate_multi_project_reports.py >> reports\scheduler.log 2>&1
echo [%date% %time%] Finished >> reports\scheduler.log 2>&1
