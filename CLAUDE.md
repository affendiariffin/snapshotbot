# SnapshotBot — CLAUDE.md

## What This Project Does

A two-part Tabletop Simulator (TTS) tool for recording and replaying Warhammer 40K games played on the **Hutber 40k Competitive 10e Map Base** mod.

1. **`GameStateSnapshot.ttslua`** — TTS Lua script that runs *inside* TTS as an additive mod, periodically capturing game state to JSON files on disk.
2. **`battlefield_visualizer.html`** — Standalone browser app that loads those JSON snapshots and renders an animated top-down battlefield replay.

---

## File Overview

| File | Language | Purpose |
|------|----------|---------|
| `GameStateSnapshot.ttslua` | TTS Lua | In-game snapshot recorder |
| `battlefield_visualizer.html` | HTML/CSS/JS (single file) | Offline snapshot viewer/player |

---

## GameStateSnapshot.ttslua

### How It Works
- **Install**: Additively loaded on top of the Hutber mod. The script lives on an invisible TTS object — no changes to Hutber's scripts needed.
- **Start detection**: Polls the `startMenu` object (GUID `738804`) every `POLL_INTERVAL` (2s) waiting for `inGame == true`.
- **On game start**: Creates a timestamped session subfolder, takes an immediate `game_start` snapshot, starts a repeating `periodic_5min` timer.
- **Snapshots**: JSON files written to `SNAPSHOT_FOLDER` (currently hardcoded to `C:/Users/User/Documents/coding_projects/TTS snapshot/`).
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
- `SNAPSHOT_INTERVAL = 300` — seconds between periodic snapshots (5 min)
- `POLL_INTERVAL = 2` — game-start poll frequency
- `START_MENU_GUID = "738804"` — GUID of Hutber's start menu object
- `SNAPSHOT_FOLDER` — output path (hardcoded, must match local machine)

### Object Filtering (battlefield_objects)
Only objects **inside the mat bounds** are captured. Excluded by tag: `ScriptingZone`, `Fog`, `FogOfWar`, `Zone`, `Hand`. Excluded by GM Notes: `deployZone`, `deployZone9`, `objective`, `areaDeny`, `quarter`, `mat_GUID`.

### Hutber Mod Integration
Reads data by GUID via `Global.getVar()`:
- `redVPCounter_GUID`, `blueVPCounter_GUID` — VP counters
- `gameTurnCounter_GUID` — round counter
- `redCPCounter_GUID`, `blueCPCounter_GUID` — CP counters
- `scoresheet_GUID` — calls `getMatchSummary()` on the scoresheet object
- `deploymentCardZone_GUID`, `primaryCardZone_GUID`, `secondary*CardZone_GUID` — mission card zones
- `mat_GUID` — the battle mat for bounds calculation

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

- **No build system** — both files are standalone. Edit and reload.
- **TTS Lua dialect** — uses `Wait.time()`, `JSON.encode_pretty()`, `printToAll()`, `getObjectFromGUID()`, and other TTS globals. Standard Lua I/O (`io.open`) works in TTS desktop.
- **Path separator** — TTS on Windows uses forward slashes in `io.open` paths.
- `SNAPSHOT_FOLDER` in the `.ttslua` file is machine-specific and must be updated when deploying to a different machine.
- The snapshot folder must exist before TTS tries to write; the session subfolder is created at game start via a `.session` marker file.
