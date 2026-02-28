@echo off
:: TTS Battlefield Replay — Build Script
:: Run this once to compile capture.pyw into TTS_Replay.exe
::
:: Requirements: Python must be installed and on PATH
:: ─────────────────────────────────────────────────────

echo.
echo  TTS Battlefield Replay — Build Script
echo  ======================================
echo.

:: Install / upgrade required packages
echo  [1/3] Installing Python dependencies...
python -m pip install --upgrade pyinstaller mss Pillow
if errorlevel 1 (
    echo.
    echo  ERROR: pip install failed. Is Python installed and on PATH?
    pause
    exit /b 1
)

echo.
echo  [2/3] Building TTS_Replay.exe with PyInstaller...
python -m PyInstaller TTS_Replay.spec --noconfirm
if errorlevel 1 (
    echo.
    echo  ERROR: PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
echo  [3/3] Done!
echo.
echo  Your exe is at:  dist\TTS_Replay.exe
echo.
echo  Share with friends:
echo    dist\TTS_Replay.exe
echo    README.txt
echo.
echo  (capture_button.lua is created automatically next to the exe on first run)
echo.
pause
