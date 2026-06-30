@echo off
REM ============================================================
REM  AiChat SSF - wipe the database (start completely from scratch).
REM  Deletes data/aichat.db (characters, chats, accounts, settings, all).
REM  ASCII-only on purpose (Cyrillic in .bat breaks cmd.exe).
REM ============================================================
setlocal
cd /d "%~dp0"

echo ============================================
echo   AiChat SSF - RESET DATABASE
echo ============================================
echo WARNING: this permanently deletes ALL data:
echo   characters, chats, groups, personas, accounts, admin/security, Telegram.
echo Close the "AiChat backend" window first, or the file will be locked.
echo.
set /p ANS="Type YES to wipe the database: "
if /I not "%ANS%"=="YES" (
  echo Cancelled.
  pause
  exit /b 0
)

if exist "data\aichat.db" (
  del /f /q "data\aichat.db"
  if exist "data\aichat.db" (
    echo [error] Could not delete data\aichat.db - is the server still running?
    echo Close the "AiChat backend" window and run this again.
    pause
    exit /b 1
  )
  echo Database deleted.
) else (
  echo No database file found - already clean.
)
REM SQLite side files (WAL/SHM), if present.
if exist "data\aichat.db-wal" del /f /q "data\aichat.db-wal"
if exist "data\aichat.db-shm" del /f /q "data\aichat.db-shm"

echo.
echo Done. A fresh empty database will be created next time you run start.bat.
pause
endlocal
