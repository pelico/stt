@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "TOOLS=F:\workbuddylog\2026-11-25-08-46-26\tools"
set "FFMPEG_EXE="
for /d %%D in ("%TOOLS%\ffmpeg-*") do if exist "%%D\bin\ffmpeg.exe" set "FFMPEG_EXE=%%D\bin\ffmpeg.exe"
set "MTX=%TOOLS%\mediamtx\mediamtx.exe"

if not defined FFMPEG_EXE (
  echo [ERROR] ffmpeg not found under %TOOLS%\ffmpeg-*
  pause
  exit /b 1
)
if not exist "%MTX%" (
  echo [ERROR] MediaMTX not found: %MTX%
  pause
  exit /b 1
)

echo [1/2] Starting MediaMTX RTSP server on :8554 ...
start "MediaMTX-RTSP" /D "%TOOLS%\mediamtx" "%MTX%"
ping -n 3 127.0.0.1 >nul

echo [2/2] Pushing test audio to rtsp://127.0.0.1:8554/live
echo      (2s tone + 3s silence loop; press Ctrl-C to stop)
"%FFMPEG_EXE%" -hide_banner -loglevel error -re -f lavfi -i "sine=frequency=300:sample_rate=16000" ^
  -af "volume='if(mod(t,5)<2,1,0)':eval=frame" ^
  -c:a aac -b:a 32k -f rtsp rtsp://127.0.0.1:8554/live
pause
