@echo off
chcp 65001 >nul
REM Weekly learning-report push. Called by Windows Task Scheduler every Sunday 21:55.
REM Manual test: double-click this file, or run: scripts\weekly_report.bat --dry-run
cd /d "%~dp0.."

REM Python interpreter - change this line only when moving to another machine
set "PY=D:\An\python.exe"
if not exist "%PY%" set "PY=python"

if not exist logs mkdir logs
echo.>> logs\weekly.log
echo [%date% %time%] ===== start weekly push =====>> logs\weekly.log
set "PYTHONIOENCODING=utf-8"
"%PY%" scripts\push_weekly_reports.py %* >> logs\weekly.log 2>&1
echo [%date% %time%] ===== done exit=%ERRORLEVEL% =====>> logs\weekly.log
