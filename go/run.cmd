@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "GOEXE=F:\workbuddylog\2026-11-25-08-46-26\tools\go\bin\go.exe"
if not exist "%GOEXE%" (
  echo [ERROR] Portable Go not found: %GOEXE%
  pause
  exit /b 1
)

if not exist "%~dp0data" mkdir "%~dp0data"
set "DB_PATH=%~dp0data\asr.db"
set "SETTINGS_PATH=%~dp0data\settings.json"
set "LOG_FILE_PATH=%~dp0data\stt.log"
set "AUDIO_DIR=%~dp0data\audio"
set "HTTP_PORT=8000"
set "GOTOOLCHAIN=local"

echo [1/2] Building stt.exe ...
"%GOEXE%" build -trimpath -ldflags="-s -w" -o stt.exe ./cmd/stt
if errorlevel 1 (
  echo [ERROR] build failed
  pause
  exit /b 1
)

echo [2/2] Starting server at http://localhost:%HTTP_PORT%
start "" "http://localhost:%HTTP_PORT%"
stt.exe
pause
