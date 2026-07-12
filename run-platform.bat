@echo off
setlocal EnableDelayedExpansion
rem ===========================================================================
rem  run-platform.bat - one click: apply pack -> warmup -> launch platform
rem  Place in provision pack root (<...>\provision\).
rem  apply only moves files; warmup installs deps offline on first run,
rem  then is fingerprint-cached (seconds) on reruns. Both are idempotent.
rem
rem  Self-test (no writes, no launch, prints plan + paths):
rem      run-platform.bat check
rem  See run-platform.README.txt (Chinese) for details.
rem ===========================================================================
rem  =====================  CONFIG - edit only this block  =====================
rem
rem  MODE = dev       local test: real platform project + prebuilt cim-light.exe
rem  MODE = portable  offline machine: standard <APP_ROOT>\ (runtime\ engine\ provision\)
set "MODE=portable"

rem  Only these tools (comma separated); empty = all tools in the pack
set "TOOLS="

rem  --- dev mode ---
set "DEV_PROJECT=C:\code\claude\nativeApp"
rem  DEV_PYTHON empty = auto-resolve via py -3.11
set "DEV_PYTHON="

rem  --- portable mode ---
rem  parent of provision\ = APP_ROOT (runtime\ and engine\ are siblings)
set "APP_ROOT=%~dp0.."
rem  ===================  end of CONFIG  ===================

set "PROV=%~dp0"
if "%TOOLS%"=="" ( set "TOOLARG=" ) else ( set "TOOLARG=--tools %TOOLS%" )

set "DRY="
set "NOLAUNCH="
if /i "%~1"=="check" (
  set "DRY=--dry-run"
  set "NOLAUNCH=1"
  echo [check] verify paths + plan only; no writes, no launch.
)

if /i "%MODE%"=="dev"      goto :dev
if /i "%MODE%"=="portable" goto :portable
echo [ERROR] MODE must be dev or portable (got: %MODE%).
goto :fail

rem ---------------------------------------------------------------------------
:dev
echo === MODE: dev (local test) ===
set "PROJECT=%DEV_PROJECT%"
set "PYTHON=%DEV_PYTHON%"
if not defined PYTHON for /f "delims=" %%p in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON=%%p"
if not defined PYTHON ( echo [ERROR] Python 3.11 not found via py -3.11. & goto :fail )
set "TAURI_DIR=%PROJECT%\apps\host-tauri\src-tauri"
set "EXE=%PROJECT%\apps\host-tauri\prebuilt\cim-light.exe"
if not exist "%EXE%" set "EXE=%TAURI_DIR%\target\release\cim-light.exe"
if not exist "%EXE%" set "EXE=%TAURI_DIR%\target\debug\cim-light.exe"
if not exist "%EXE%" ( echo [ERROR] cim-light.exe not found. & goto :fail )
set "ENGINE_PY=%PROJECT%\sidecar\python-engine\engine.py"
if not exist "%ENGINE_PY%" ( echo [ERROR] engine.py not found: %ENGINE_PY% & goto :fail )
rem  deppack-cache / tool-venvs kept self-contained next to the pack
set "DEPPACK_CACHE=%PROV%_run\deppack-cache"
set "TOOL_VENVS=%PROV%_run\tool-venvs"

echo project : %PROJECT%
echo python  : %PYTHON%
echo exe     : %EXE%

call :apply
if errorlevel 1 goto :fail
call :warmup
if errorlevel 1 goto :fail
if defined NOLAUNCH goto :done

echo.
echo [launch] cim-light.exe (dev). When the window opens: pick a tool -^> Start.
start "CIM Platform (provision dev)" cmd /k "cd /d %TAURI_DIR% && set CIM_ENGINE_EXE=%ENGINE_PY%&& set CIM_ENGINE_PYTHON=%PYTHON%&& set CIM_DEPPACK_CACHE=%DEPPACK_CACHE%&& set CIM_TOOL_VENVS_DIR=%TOOL_VENVS%&& set CIM_REPO_ROOT=%PROJECT%&& set PYTHONUTF8=1&& %EXE%"
goto :done

rem ---------------------------------------------------------------------------
:portable
echo === MODE: portable (offline machine) ===
for %%i in ("%APP_ROOT%") do set "APP_ROOT=%%~fi"
set "PYTHON=%APP_ROOT%\runtime\python311\python.exe"
set "PROJECT=%APP_ROOT%\engine"
set "LAUNCH=%APP_ROOT%\start.bat"
if not exist "%PYTHON%" ( echo [ERROR] portable Python not found: %PYTHON% & goto :fail )
if not exist "%PROJECT%\sidecar\python-engine\engine.py" ( echo [ERROR] platform engine not found: %PROJECT% & goto :fail )
if not exist "%LAUNCH%" ( echo [ERROR] start.bat not found: %LAUNCH% & goto :fail )
set "DATAKEY="
for /d %%d in ("%APP_ROOT%\data\*") do set "DATAKEY=%%d"
if not defined DATAKEY (
  echo [NOTE] No data\^<project-key^>\ yet. Run start.bat once so it appears, then rerun this bat.
  echo        ^(Running it once also confirms the platform itself launches.^)
  goto :fail
)
set "DEPPACK_CACHE=%DATAKEY%\deppack-cache"
set "TOOL_VENVS=%DATAKEY%\tool-venvs"

echo APP_ROOT : %APP_ROOT%
echo project  : %PROJECT%
echo cache    : %DEPPACK_CACHE%

call :apply
if errorlevel 1 goto :fail
call :warmup
if errorlevel 1 goto :fail
if defined NOLAUNCH goto :done

echo.
echo [launch] start.bat. When the window opens: pick a tool -^> Start.
call "%LAUNCH%"
goto :done

rem ---------------------------------------------------------------------------
:apply
echo.
echo [1/2] apply - move wheels into deppack-cache
echo        target: %DEPPACK_CACHE%
"%PYTHON%" "%PROV%apply.py" --deppack-cache "%DEPPACK_CACHE%" %TOOLARG% %DRY%
exit /b %errorlevel%

:warmup
echo.
echo [2/2] warmup - install deps offline into per-tool venv (slow first time, cached after)
if defined DRY (
  echo        [check] skipping real warmup. Real command would be:
  echo        "%PYTHON%" "%PROV%warmup.py" --project "%PROJECT%" --deppack-cache "%DEPPACK_CACHE%" --tool-venvs "%TOOL_VENVS%" %TOOLARG%
  exit /b 0
)
"%PYTHON%" "%PROV%warmup.py" --project "%PROJECT%" --deppack-cache "%DEPPACK_CACHE%" --tool-venvs "%TOOL_VENVS%" %TOOLARG%
exit /b %errorlevel%

rem ---------------------------------------------------------------------------
:fail
echo.
echo *** FAILED - aborted. ***
endlocal & exit /b 1

:done
echo.
if defined NOLAUNCH (echo check done: paths and plan look good.) else (echo done. platform launched.)
endlocal & exit /b 0
