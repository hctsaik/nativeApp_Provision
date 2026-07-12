@echo off
rem ============================================================================
rem  start.bat - the ONLY thing the end user runs.
rem
rem  Deliberately dumb: locate the package root and hand over to the bundled
rem  Python. Port selection, process supervision, health checks, logging and
rem  cleanup all live in launcher\launch.py, where they can be tested.
rem ============================================================================
setlocal
chcp 65001 >nul 2>&1
title 應用程式啟動中 - 請不要關閉這個視窗

rem pushd (not `cd /d`) so a UNC path (\\server\share\app) works: `cd /d` silently
rem fails there, leaves you in C:\Windows, and every message after it is a lie.
pushd "%~dp0" || (
  echo [start][ERROR] 無法進入程式資料夾:%~dp0
  echo               若是從網路磁碟機執行,請先把整個資料夾複製到本機磁碟。
  pause
  exit /b 1
)

if not exist "runtime\python.exe" (
  echo [start][ERROR] 這個資料夾不完整:找不到 runtime\python.exe
  echo               請向提供者重新索取完整的資料夾。
  popd
  pause
  exit /b 1
)

rem WebView2 是這個資料夾裡「唯一」需要事先裝在系統上的東西:視窗是它畫的。
rem 缺了它,Streamlit 會正常起來、殼會在一秒內死掉、User 只看到一個閃過的黑窗。
rem 與其讓他等一分鐘再失望,不如在開跑前就講清楚,並指向那個真的存在的安裝檔。
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>&1
if not errorlevel 1 goto webview2_ok
reg query "HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>&1
if not errorlevel 1 goto webview2_ok
echo.
echo [start][ERROR] 這台電腦缺 Microsoft Edge WebView2 Runtime,應用視窗開不起來。
echo.
echo   請先執行:tools\安裝WebView2.bat
echo   (可用一般使用者權限安裝,不需要系統管理員。裝完再跑一次 start.bat。)
echo.
popd
pause
exit /b 5
:webview2_ok

"runtime\python.exe" "launcher\launch.py" %*
set "RC=%errorlevel%"
popd

if not "%RC%"=="0" (
  echo.
  echo [start] 啟動失敗（代碼 %RC%）。詳細記錄在 data\logs\ 資料夾裡。
  pause
)
exit /b %RC%
