@echo off
setlocal
if /i "%~1"=="--console" goto launch
if /i "%~1"=="--check" goto check
"%ComSpec%" /d /k call "%~f0" --console
exit /b

:launch
title ZForge - Local Workbench
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ZFORGE_PY=python"
where py >nul 2>nul
if not errorlevel 1 (
  py -3.13 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "ZFORGE_PY=py -3.13"
)
echo [ZForge] Starting. Please keep this window open.
%ZFORGE_PY% -u -X utf8 "05_Web服务\run.py"
set "ZFORGE_EXIT=%ERRORLEVEL%"
if not "%ZFORGE_EXIT%"=="0" echo [ZForge] Failed. See 05_Web服务\data\startup.log and the error above.
echo [ZForge] The launcher has returned. This window stays open.
echo [ZForge] Close this window when finished, or launch again to retry.
exit /b %ZFORGE_EXIT%

:check
cd /d "%~dp0"
python -u -X utf8 "05_Web服务\run.py" --check
exit /b %ERRORLEVEL%
