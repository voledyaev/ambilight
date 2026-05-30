@echo off
rem Double-clickable launcher for ambilight on Windows.
rem Finds uv, then runs the app from this folder. Any flags you append
rem to this file's shortcut (or pass on the command line) are forwarded.
setlocal
cd /d "%~dp0"

rem uv is normally on PATH after install; fall back to its default home.
where uv >nul 2>nul
if errorlevel 1 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo   ERROR: 'uv' was not found on this system.
    echo   Install it from https://docs.astral.sh/uv/  then run this again.
    echo.
    pause
    exit /b 1
)

echo Starting ambilight...  ^(close this window or press Ctrl+C to stop^)
echo.
uv run ambilight %*

echo.
echo ambilight stopped.
pause
endlocal
