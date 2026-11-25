@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "GOEXE=F:\workbuddylog\2026-11-25-08-46-26\tools\go\bin\go.exe"
set "TOOLS=F:\workbuddylog\2026-11-25-08-46-26\tools"
set "FFMPEG_EXE="
for /d %%D in ("%TOOLS%\ffmpeg-*") do if exist "%%D\bin\ffmpeg.exe" set "FFMPEG_EXE=%%D\bin\ffmpeg.exe"
if not exist "%GOEXE%" (
  echo [ERROR] Portable Go not found: %GOEXE%
  pause
  exit /b 1
)
if not defined FFMPEG_EXE (
  echo [ERROR] ffmpeg not found under %TOOLS%\ffmpeg-*
  pause
  exit /b 1
)

if not exist "%~dp0data" mkdir "%~dp0data"
set "DB_PATH=%~dp0data\asr.db"
set "SETTINGS_PATH=%~dp0data\settings.json"
set "LOG_FILE_PATH=%~dp0data\stt.log"
set "AUDIO_DIR=%~dp0data\audio"
set "HTTP_PORT=8000"
set "FFMPEG_PATH=%FFMPEG_EXE%"
set "GOTOOLCHAIN=local"
rem Avoid routing domestic streams through an injected proxy (causes 403).
rem Delete the next 4 lines if you actually need a proxy for your stream.
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="

echo [1/2] Building stt.exe ...
"%GOEXE%" build -trimpath -ldflags="-s -w" -o stt.exe ./cmd/stt
if errorlevel 1 (
  echo [ERROR] build failed
  pause
  exit /b 1
)

echo [2/2] Starting server at http://localhost:%HTTP_PORT%
echo      ffmpeg: %FFMPEG_EXE%
start "" "http://localhost:%HTTP_PORT%"
stt.exe
pause
