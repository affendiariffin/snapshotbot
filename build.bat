@echo off
echo ============================================================
echo  SnapshotBot -- one-time build
echo ============================================================
echo.

echo [1/2] Installing Python dependencies...
py -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip failed. Make sure Python is installed.
    pause
    exit /b 1
)

echo.
echo [2/2] Compiling SnapshotBot.exe...
py -m PyInstaller --onefile --windowed --name SnapshotBot snapshot_server.py
if errorlevel 1 (
    echo ERROR: PyInstaller failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done!
echo  Your executable is at:  dist\SnapshotBot.exe
echo  Move it anywhere you like -- it has no external dependencies.
echo  Double-click it before launching TTS each session.
echo ============================================================
pause
