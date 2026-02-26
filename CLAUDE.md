# SnapshotBot — CLAUDE.md

## What This Project Does

A three-part Tabletop Simulator (TTS) tool for recording and replaying Warhammer 40K games played on the **Hutber 40k Competitive 10e Map Base** mod.

1. **`GameStateSnapshot.ttslua`** — TTS Lua script that runs *inside* TTS as an additive mod, periodically capturing game state and POSTing it via HTTP to the local server.
2. **`snapshot_server.py`** — Local Windows tray app (compiled to `SnapshotBot.exe`) that receives snapshots from TTS and writes them as JSON files to disk.
3. **`battlefield_visualizer.html`** — Standalone browser app that loads those JSON snapshots and renders an animated top-down battlefield replay.

---

## File Overview

| File | Language | Purpose |
|------|----------|---------|
| `GameStateSnapshot.ttslua` | TTS Lua | In-game snapshot recorder |
| `snapshot_server.py` | Python | Local HTTP server / system tray app |
| `build.bat` | Batch | One-time script to compile `snapshot_server.py` → `SnapshotBot.exe` |
| `requirements.txt` | pip | Python dependencies: `pystray`, `pillow`, `pyinstaller` |
| `battlefield_visualizer.html` | HTML/CSS/JS (single file) | Offline snapshot viewer/player |

---

## Architecture

TTS's Lua sandbox (MoonSharp) strips the entire `io` library — `io.open` is nil and cannot be used for file I/O from any script. The workaround is:

```
GameStateSnapshot.ttslua  →  WebRequest.custom() HTTP POST  →  SnapshotBot.exe  →  .json file on disk
                               localhost:39999/snapshot
```

`SnapshotBot.exe` must be running before TTS is launched. It shows a green camera icon in the Windows system tray. Right-click → **Open Snapshot Folder** or **Quit**.

---

## GameStateSnapshot.ttslua

### How It Works
- **Install**: Additively loaded on top of the Hutber mod. The script lives on a TTS object — no changes to Hutber's scripts needed.
- **On load**: Fires a GET to `localhost:39999/health` and prints `SnapshotBot.exe connected OK.` or a warning to TTS chat.
- **Start detection**: Polls the `startMenu` object (GUID `738804`) every `POLL_INTERVAL` (2s) waiting for `inGame == true`.
- **On game start**: Takes an immediate `game_start` snapshot, starts a repeating `periodic_1min` timer.
- **Snapshots**: Serialized with a pure-Lua `serialize()` function (no `JSON` global — it's nil in object scripts), then POSTed to `SERVER_URL` with the filename in the `X-Filename` header.
- **Cleanup**: `onDestroy()` stops all timers when the object is destroyed or the table unloads.

### Snapshot JSON Schema
```
{
  meta: { reason, timestamp, snapshot_index, mod_name }
  game: { round, current_turn, first_player, game_mode, singles_mode }
  mission: { deployment, primary, red_secondaries[], blue_secondaries[],
             mission_pack, map_pack, map_name, map_variant, map_id,
             map_layout, map_key }
  players: { red: { steam_name, steam_id }, blue: { ... } }
  scores: {
    red: { total, cp, primary, secondary, challenger, rounds[] }
    blue: { ... }
  }
  battlefield_objects: [
    { guid, name, tag, position: {x,z}, rotation_y, size: {x,z},
      locked, description }
  ]
}
```

### Key Constants / Config
- `SNAPSHOT_INTERVAL = 60` — seconds between periodic snapshots (1 min)
- `POLL_INTERVAL = 2` — game-start poll frequency
- `START_MENU_GUID = "738804"` — GUID of Hutber's start menu object
- `SERVER_URL = "http://localhost:39999/snapshot"` — SnapshotBot.exe endpoint

### Object Filtering (battlefield_objects)
Only objects **inside the mat bounds** are captured. Excluded by tag: `ScriptingZone`, `Fog`, `FogOfWar`, `Zone`, `Hand`. Excluded by GM Notes: `deployZone`, `deployZone9`, `objective`, `areaDeny`, `quarter`, `mat_GUID`.

### WebRequest.custom() parameter order
TTS expects headers **before** the callback:
```lua
WebRequest.custom(url, method, download, data, headers_table, callback_function)
```
Passing callback before headers throws: `cannot convert a function to a clr type Dictionary<string,string>`.

### Hutber Mod Integration
Reads data by GUID via `Global.getVar()`:
- `redVPCounter_GUID`, `blueVPCounter_GUID` — VP counters
- `gameTurnCounter_GUID` — round counter
- `redCPCounter_GUID`, `blueCPCounter_GUID` — CP counters
- `scoresheet_GUID` — calls `getMatchSummary()` on the scoresheet object
- `deploymentCardZone_GUID`, `primaryCardZone_GUID`, `secondary*CardZone_GUID` — mission card zones
- `mat_GUID` — the battle mat for bounds calculation

---

## snapshot_server.py / SnapshotBot.exe

- Listens on `localhost:39999` only (not network-accessible)
- `POST /snapshot` — reads `X-Filename` header, writes body to `SAVE_DIR`
- `GET /health` — returns 200 OK (used by Lua health check on load)
- `SAVE_DIR = ~/Documents/TTS Snapshots` (resolves to current user's home, works on any machine)
- Tray icon tooltip updates with snapshot count after each save
- Compiled with PyInstaller `--onefile --windowed` → fully self-contained, no Python needed to run

### Building
Run `build.bat` once. Requires Python installed with `py` launcher on PATH (Microsoft Store Python works). Output: `dist\SnapshotBot.exe`.

### Sharing
- The `.exe` is self-contained — friend does not need Python
- Windows SmartScreen will show "Windows protected your PC" on first run — click **More info → Run anyway**

---

## battlefield_visualizer.html

### How It Works
- Pure client-side HTML/JS, no build step, no server needed — open directly in a browser.
- Load snapshots via drag & drop or the "Load JSON" button (multiple files supported).
- Snapshots are sorted by `meta.snapshot_index` and displayed on a timeline.

### Canvas Rendering Layers (in draw order)
1. **Background** — dark green grid with deployment zone tinting
2. **Terrain** — hatched brown rectangles, sized from `obj.size`, rotated from `rotation_y`
3. **Objectives** — gold hexagons with glow
4. **Units** — red team = diamond shape, blue team = circle; highlighted unit gets dashed glow ring

### Team Detection
Heuristic only: objects left of the battlefield midpoint (X axis) = blue, right = red. There is no reliable team data in the snapshot.

### Object Classification
- `isTerrainObj()`: `description === 'terrain'` OR (`locked === true` AND name matches terrain keywords)
- `isObjectiveObj()`: `description === 'objective'` OR name includes "objective"
- Everything else = unit

### Timeline / Playback
- `globalBounds` is computed once across all loaded snapshots so the view stays stable during playback.
- Playback speeds: 0.5×, 1×, 2×, 4×, 10× (configurable via dropdown).
- Keyboard: `Space` = play/pause, `←`/`→` = step.

### Fonts
Loads from Google Fonts: `Rajdhani` (UI) and `Share Tech Mono` (monospace labels). Requires internet for first load; falls back to system sans-serif/monospace.

---

## Development Notes

- **No build system** — all files are standalone. Edit and reload.
- **TTS Lua sandbox** — MoonSharp strips `io`, `os.execute`, and `JSON` globals. File I/O must go via `WebRequest` to a local server. `JSON` global is nil in object scripts (works in Global script only). Use the pure-Lua `serialize()` / `deepCopy()` functions already in the script.
- **TTS Lua `goto`** — forbidden when jumping over local variable declarations; use nested `if` blocks instead.
- **Cross-script tables** — `obj.call()` and `obj.getVar()` return cross-script tables; always `deepCopy()` before use.
