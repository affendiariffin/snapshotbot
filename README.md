# TTS Battlefield Replay — Setup Guide

## Prerequisites

```
pip install mss Pillow pystray opencv-python-headless numpy
```

---

## First-Time Setup

### 1. Start the tray app
Double-click `capture.py` (or run `python capture.py`).
A red circle icon appears in your **Windows system tray** (bottom-right).
No terminal window — everything runs from the tray icon.

### 2. Calibrate your capture region
Right-click the tray icon → **Calibrate Region**.
A dark overlay appears — drag a rectangle over the battlefield area
in your TTS window, then release. The region is saved to `config.json`.

### 3. Set up the TTS Lua button
In TTS, right-click any object → **Scripting** → paste the contents of
`capture_button.lua`. Edit the two settings at the top:

| Setting | What to set |
|---|---|
| `PLAYER_COLOR` | Your TTS colour e.g. `"White"`, `"Red"` |
| `TOP_DOWN_POSITION` | X/Z coordinates of your battlefield centre |
| `TOP_DOWN_DISTANCE` | Zoom level — increase for larger boards |

To find your battlefield centre coordinates, position TTS overhead,
open the Lua console (Modding → Scripting) and run:
```lua
print(Player["White"].getPosition())
```

---

## Every Game Session

1. Right-click tray icon → **Start Session**
   The icon status changes to **● ACTIVE — waiting for TTS**

2. Play your game normally.

3. At the end of each turn, click the **📷 CAPTURE TURN** button in TTS.
   What happens automatically:
   - Camera snaps to top-down
   - Python waits 500ms for it to settle
   - Screenshot is grabbed and saved
   - Drawing lines are filtered out
   - Camera returns to normal 3D view
   - A green "Turn captured ✓" message appears in TTS chat

4. After the game, right-click tray icon → **Open Replay in Browser**

---

## Tray Menu Reference

| Menu Item | Action |
|---|---|
| ● ACTIVE / ○ Stopped | Status indicator (not clickable) |
| Calibrate Region | Drag to set capture area |
| Start Session | Begin listening for TTS signals (new session) |
| Stop Session | Stop listening |
| Open Replay in Browser | Opens playback.html at localhost:8080 |
| Clean Existing Frames | Re-run drawing line filter on saved frames |
| Exit | Close the app |

---

## Replay Keyboard Shortcuts

| Key | Action |
|---|---|
| Space | Play / Pause |
| ← | Previous frame |
| → | Next frame |
| Home | First frame |
| End | Last frame |

---

## File Layout

```
tts_capture/
├── capture.py           ← tray app (run this)
├── capture_button.lua   ← paste into TTS object scripting
├── playback.html        ← replay viewer
├── config.json          ← saved crop region (auto-created)
└── screenshots/
    ├── manifest.json
    ├── frame_0000.png
    ├── frame_0001.png
    └── ...
```

---

## Troubleshooting

**"Cannot bind port 39998"**
Another app (e.g. the TTS Atom plugin) is already using that port.
Close it, or change `TTS_LISTEN_PORT` in `capture.py`.

**Button appears but nothing happens**
Make sure the tray app is running and Session is started before pressing
the button in TTS.

**Camera doesn't return to 3D**
Increase the `0.8` second delay in `capture_button.lua` line 4 of `doCapture`.
