@echo off
:: Fendi's Snapshotbot — Build Script
:: Run this once to compile "Fendi's Snapshotbot.pyw" into "Fendi's Snapshotbot.exe"
::
:: Requirements: Python must be installed and on PATH
:: ─────────────────────────────────────────────────────

echo.
echo  Fendi's Snapshotbot — Build Script
echo  ====================================
echo.

:: Install / upgrade required packages
echo  [1/3] Installing Python dependencies...
python -m pip install --upgrade pyinstaller mss Pillow opencv-python-headless numpy
if errorlevel 1 (
    echo.
    echo  ERROR: pip install failed. Is Python installed and on PATH?
    pause
    exit /b 1
)

echo.
echo  [2/3] Building exe with PyInstaller...
python -m PyInstaller Fendis_Snapshotbot.spec --noconfirm
if errorlevel 1 (
    echo.
    echo  ERROR: PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
echo  [3/3] Done!
echo.
echo  Your exe is at:  dist\Fendi's Snapshotbot.exe
echo.
echo  Share with friends:
echo    dist\Fendi's Snapshotbot.exe
echo.
echo  (capture_button.lua is created automatically next to the exe on first run)
echo.
pause
