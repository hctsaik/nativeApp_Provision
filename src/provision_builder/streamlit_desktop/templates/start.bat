@echo off
rem ============================================================================
rem  start.bat - the ONLY thing the end user runs.
rem
rem  Deliberately dumb: locate the package root and hand over to the bundled
rem  Python. Port selection, process supervision, health checks, logging and
rem  cleanup all live in launcher\launch.py, where they can be tested.
rem
rem  THIS FILE IS PURE ASCII, ON PURPOSE. DO NOT PUT CHINESE IN IT.
rem
rem  Under `chcp 65001` cmd.exe tracks its position in a .bat as a BYTE offset but
rem  computes it by counting CHARACTERS. Every time it has to re-read the file --
rem  after a `for /f`, a pipe, an external command, a `goto` -- it seeks to an
rem  offset that is wrong by however many multi-byte characters came before, lands
rem  in the MIDDLE of a line, and executes whatever it finds. We have watched it
rem  execute the tail of a Chinese `rem` comment. It is nondeterministic: roughly
rem  1 run in 20, which is exactly how it survived review and shipped.
rem
rem  In an ASCII-only file, byte offset == character offset, so the seek cannot
rem  miss. That is the whole trick, and it also subsumes the old em-dash rule.
rem  Operator-facing Traditional Chinese lives in messages\*.txt and is printed
rem  with `type`, which hands the bytes to the console and never parses them.
rem  See builder._write_bat (it rejects a non-ASCII .bat) and _write_messages.
rem ============================================================================
setlocal
chcp 65001 >nul 2>&1

rem The title is Chinese too, so it is read from a file rather than written here.
set "TITLE="
if exist "%~dp0messages\title.txt" set /p TITLE=<"%~dp0messages\title.txt"
if defined TITLE title %TITLE%

rem pushd (not `cd /d`) so a UNC path (\server\share\app) works: `cd /d` silently
rem fails there, leaves you in C:\Windows, and every message after it is a lie.
pushd "%~dp0" || (
  echo [start][ERROR] cannot enter the program folder: %~dp0
  type "%~dp0messages\start-nofolder.txt" 2>nul
  pause
  exit /b 1
)

if not exist "runtime\python.exe" (
  echo [start][ERROR] runtime\python.exe is missing
  type "messages\start-incomplete.txt" 2>nul
  popd
  pause
  exit /b 1
)

rem WebView2 is the ONE thing that must already be on the machine: it draws the
rem window. Without it Streamlit comes up fine, the shell dies within a second,
rem and the user sees a black window flash past. Say so BEFORE the minute of
rem waiting, not after.
rem
rem This is the single source of truth for "is WebView2 here", so all THREE
rem locations get queried. Miss one and a machine that HAS it is turned away:
rem   WOW6432Node : per-machine install on x64
rem   native path : per-machine install on ARM64 / x86
rem   HKCU        : per-user install (no admin needed) appears nowhere else
rem A pv of 0.0.0.0 is what a broken or partial install leaves behind -- the key
rem is there, the runtime is not. That counts as NOT installed.
set "WV2PV="
for /f "tokens=1,2,*" %%U in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv 2^>nul') do if /i "%%U"=="pv" if not "%%W"=="0.0.0.0" set "WV2PV=%%W"
for /f "tokens=1,2,*" %%U in ('reg query "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv 2^>nul') do if /i "%%U"=="pv" if not "%%W"=="0.0.0.0" set "WV2PV=%%W"
for /f "tokens=1,2,*" %%U in ('reg query "HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv 2^>nul') do if /i "%%U"=="pv" if not "%%W"=="0.0.0.0" set "WV2PV=%%W"

rem No parentheses in an echo INSIDE a block: an unescaped `)` closes the block
rem early. `echo ... (exit 5)` here made the WebView2 error print unconditionally
rem and turned away every machine, WebView2 or not. builder._write_bat rejects it.
if not defined WV2PV (
  echo [start][ERROR] Microsoft Edge WebView2 Runtime is missing. exit code 5.
  type "messages\start-webview2.txt" 2>nul
  popd
  pause
  exit /b 5
)

"runtime\python.exe" "launcher\launch.py" %*
set "RC=%errorlevel%"
popd

if not "%RC%"=="0" (
  echo [start][ERROR] exit code %RC%
  type "%~dp0messages\start-failed.txt" 2>nul
  pause
)
exit /b %RC%
