# Fendi's Snapshotbot

A tool for recording and replaying Tabletop Simulator (TTS) wargame sessions as self-contained HTML files, complete with scores, mission cards, and a doodle overlay.

## What It Does

- Listens for capture signals from a TTS object script
- Positions the TTS camera top-down before each capture
- Takes a screenshot of the battlefield region
- Removes TTS drawing-tool lines automatically (OpenCV inpainting)
- Reads live scores and mission cards from the game
- At the end of a session, packages everything into a single HTML replay file

## Requirements

- Windows (uses Win32 APIs for monitor detection)
- Tabletop Simulator running on the same machine
- The Hutber Wargaming mod (specific scoresheet and zone GUIDs are hardcoded)

## Installation

### Option A — Run the exe (recommended)

Download `Fendi's Snapshotbot.exe` from the `dist/` folder. No Python required.

### Option B — Run from source

```
pip install mss Pillow opencv-python-headless numpy
python "Fendi's Snapshotbot.pyw"
```

## Setup (first run)

1. **Start the app** — a small window appears in your taskbar.
2. **In TTS**, right-click any object → Scripting → paste the contents of `capture_button.lua` (created automatically next to the exe) → Save & Play.
3. **Calibrate the region** (optional but recommended):
   - Click "📐 CALIBRATE" on the in-game object — the camera locks top-down for 15 seconds.
   - Alt-tab to the Snapshotbot window and click **Calibrate Region**.
   - Drag a rectangle over the battlefield in the transparent overlay, then release.
   - The region is saved to `replay_config.json` and used for all future captures.
   - Without calibration, the tool captures the entire monitor TTS is on.

## Usage

### Starting a session

Click **▶ Start Session** in the Snapshotbot window. The indicator turns green and the app connects to TTS.

### Capturing manually

Click **📷 CAPTURE** on the in-game object. The camera swings top-down, waits for the image to stabilise, takes the screenshot, then returns to third-person.

### Auto-capture (recording mode)

Click **⏺ START REC** on the in-game object. The app captures automatically every 60 seconds. Click **⏹ STOP REC** to end recording mode (the session continues until you stop it from the Python window).

### Ending a session

Click **■ Stop Session** in the Snapshotbot window. The app:
1. Encodes all captured frames as base64 JPEG
2. Writes a single self-contained HTML file to `Snapshotbot Replays/`
3. Opens the file in your browser automatically

## The HTML Replay

The output file contains:

- **Frame slideshow** — arrow keys or slider to navigate; spacebar to play/pause
- **Score panel** — primary / secondary / challenger / total per player, per round
- **Shared cards** — deployment, primary, challenger, and secondary objective names
- **Doodle overlay** — draw red/blue annotations over any frame (undo, clear frame)
- **Game Notebook** — text areas for Deployment and each of 5 rounds; click **Save & Download** to embed your notes into the HTML for sharing

## Building the exe

Run `build.bat`. Requires Python and pip on PATH.

```
build.bat
```

Output: `dist/Fendi's Snapshotbot.exe`

Dependencies installed by the build script: `pyinstaller mss Pillow opencv-python-headless numpy`

## File Layout

```
Fendi's Snapshotbot.pyw     Python source (single file)
Fendis_Snapshotbot.spec     PyInstaller spec
build.bat                   Build script
dist/
  Fendi's Snapshotbot.exe   Compiled executable
Snapshotbot Replays/        Session output folder (created at runtime)
  Game Notebook session_YYYYMMDD_HHMMSS.html   ← replay files end up here
capture_button.lua          Written on first run — paste into TTS
replay_config.json          Saved calibration region
capture_debug.log           Debug log (overwritten each run)
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "TTS not reachable on port 39999" | TTS is not running, or its External Editor API is disabled |
| Camera doesn't move on capture | The player whose colour you clicked is not seated |
| Blank frames / wrong region | Re-run calibration; drag the box more carefully over the battlefield |
| Drawing lines not removed | Lines must be one of the four TTS drawing colours (red, blue, teal, purple) |
| Scores show 0 | Scoresheet GUID `06d627` not found — this mod's scoresheet may have changed |
