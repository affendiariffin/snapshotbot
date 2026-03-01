"""TTS Battlefield Replay — capture.pyw

Listens for capture signals from a TTS Lua script, takes screenshots of a
calibrated screen region, strips drawing lines, and compiles a self-contained
HTML replay file when the session ends.

A small window sits on the taskbar with session controls.

Requirements (for running from source):
  pip install mss Pillow opencv-python-headless numpy
"""

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR     = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
STORE_DIR   = APP_DIR / "TTS Replay Sessions"
CONFIG_FILE = APP_DIR / "replay_config.json"

# ── Constants ─────────────────────────────────────────────────────────────────

TTS_LISTEN_PORT        = 39998   # TTS External Editor API — TTS is the server
TTS_RECONNECT_INTERVAL = 3       # seconds between connection attempts

CAMERA_SETTLE_MS    = 500   # ms to wait before first stability sample
STABILITY_POLL_MS   = 150   # ms between samples
STABILITY_MAX_POLLS = 8     # give up after this many unstable polls
STABILITY_THRESHOLD = 0.995 # fraction of pixels that must match to call it stable

# ── Drawing-line exact RGB values ─────────────────────────────────────────────
# Each entry is (label, (R, G, B)).

DRAWING_COLORS_RGB = [
    ("red",    (218,  22,  22)),
    ("blue",   ( 28, 135, 255)),
    ("teal",   ( 34, 177, 155)),
    ("purple", (255,   0, 255)),
]

# Per-channel tolerance for PNG compression rounding.  0 = pixel-perfect.
RGB_TOLERANCE  = 4
INPAINT_RADIUS = 5

# ── Shared app state ──────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_state = {
    "listening":   False,
    "frame_num":   0,
    "manifest":    None,
    "session_dir": None,
    "window":      None,
    "status_var":  None,
    "btn_session": None,
    "indicator":   None,
}

# ── Embedded assets ───────────────────────────────────────────────────────────

CAPTURE_BUTTON_LUA = r"""-- TTS Battlefield Replay — Capture Button
-- Attach this script to any object in your TTS save.
-- Right-click object → Scripting → paste → Save & Play.
--
-- Set TOP_DOWN_POSITION to the centre of your battlefield (X, Y, Z).
-- Set TOP_DOWN_DISTANCE to control zoom — increase for larger boards.
-- Set CAPTURE_INTERVAL to change how often auto-capture fires (seconds).

local TOP_DOWN_POSITION = {
    x = 0,    -- centre of battlefield (X axis)
    y = 10,   -- height above table
    z = 0,    -- centre of battlefield (Z axis)
}

local TOP_DOWN_DISTANCE  = 40
local CAPTURE_INTERVAL   = 60    -- seconds between auto-captures

-- ─────────────────────────────────────────────────────────────────────────────

local recording       = false
local recorder_color  = nil
local capturing       = false   -- true while a capture sequence is in flight

-- Button indices (TTS assigns them in creation order, 0-based)
local BTN_CAPTURE = 0
local BTN_REC     = 1

-- ─────────────────────────────────────────────────────────────────────────────

function onLoad()
    self.createButton({
        label          = "📷 CAPTURE",
        click_function = "doCapture",
        function_owner = self,
        position       = {0, 0.1, -0.5},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {1, 0.85, 0.2},
        tooltip        = "Capture this moment",
    })

    self.createButton({
        label          = "⏺ START REC",
        click_function = "doToggleRec",
        function_owner = self,
        position       = {0, 0.1, 0.5},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {0.9, 0.2, 0.2},
        tooltip        = "Auto-capture every " .. CAPTURE_INTERVAL .. "s",
    })
end

-- ─────────────────────────────────────────────────────────────────────────────
-- Data collection — all routed through the scoresheet to avoid TTS cross-script
-- ownership errors when reading zone objects.
-- ─────────────────────────────────────────────────────────────────────────────

local SCORESHEET_GUID = "06d627"

local function getScores()
    local sheet = getObjectFromGUID(SCORESHEET_GUID)
    if not sheet then return nil end
    local ok, result = pcall(function() return sheet.call("getMatchSummary") end)
    if ok then return result end
    return nil
end

local function getCards()
    local sheet = getObjectFromGUID(SCORESHEET_GUID)
    if not sheet then return nil end
    local ok, result = pcall(function() return sheet.call("getCardData") end)
    if ok then return result end
    return nil
end

local function runSequence(player_color, action_name)
    if capturing then return end
    local player = Player[player_color]
    if player == nil then return end

    capturing = true
    player.setCameraMode("TopDown")
    player.lookAt({
        position = TOP_DOWN_POSITION,
        pitch    = 90,
        distance = TOP_DOWN_DISTANCE,
    })

    Wait.time(function()
        sendExternalMessage({ action = action_name, scores = getScores(), cards = getCards() })
        Wait.time(function()
            player.setCameraMode("ThirdPerson")
            capturing = false
        end, 0.8)
    end, 1.0)
end

-- ─────────────────────────────────────────────────────────────────────────────

function doCapture(obj, player_color, alt_click)
    if not capturing then
        runSequence(player_color, "capture")
    end
end

-- ─────────────────────────────────────────────────────────────────────────────

function doToggleRec(obj, player_color, alt_click)
    if recording then
        recording = false
        self.editButton({
            index      = BTN_REC,
            label      = "⏺ START REC",
            font_color = {0.9, 0.2, 0.2},
        })
    else
        recording      = true
        recorder_color = player_color
        self.editButton({
            index      = BTN_REC,
            label      = "⏹ STOP REC",
            font_color = {1, 0.4, 0.0},
        })
        runSequence(recorder_color, "capture_auto")
        scheduleNextCapture()
    end
end

function scheduleNextCapture()
    Wait.time(function()
        if not recording then return end
        runSequence(recorder_color, "capture_auto")
        scheduleNextCapture()
    end, CAPTURE_INTERVAL)
end

-- ─────────────────────────────────────────────────────────────────────────────

function onExternalMessage(data)
    if data and data.action == "handshake" then
        broadcastToAll("[Snapshot Bot] Connected", {0.18, 0.8, 0.42})
    end
end
"""


# ── First-run setup ───────────────────────────────────────────────────────────

def ensure_assets():
    """Write capture_button.lua on first run if missing."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    lua_path = APP_DIR / "capture_button.lua"
    if not lua_path.exists():
        lua_path.write_text(CAPTURE_BUTTON_LUA, encoding="utf-8")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def notify(title, msg):
    """Update the status bar label; safe to call from any thread."""
    win = _state.get("window")
    if win and win.winfo_exists():
        win.after(0, lambda: _state["status_var"].set(msg))

# ── Image filter ──────────────────────────────────────────────────────────────

def strip_drawing_lines(filepath: Path) -> bool:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False

    img_bgr = cv2.imread(str(filepath))
    if img_bgr is None:
        return False

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.int16)
    h, w    = img_bgr.shape[:2]
    mask    = np.zeros((h, w), dtype=np.uint8)

    for _label, (r, g, b) in DRAWING_COLORS_RGB:
        target = np.array([r, g, b], dtype=np.int16)
        diff   = np.abs(img_rgb - target)
        hit    = np.all(diff <= RGB_TOLERANCE, axis=2)
        mask[hit] = 255

    if int(np.count_nonzero(mask)) == 0:
        return False

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask   = cv2.dilate(mask, kernel, iterations=2)
    result = cv2.inpaint(img_bgr, mask, INPAINT_RADIUS, cv2.INPAINT_TELEA)
    cv2.imwrite(str(filepath), result)
    return True

# ── Screenshot ────────────────────────────────────────────────────────────────

def take_screenshot(prefetched_monitor: dict | None = None,
                    scores: dict | None = None,
                    cards:  dict | None = None):
    try:
        import mss
        from mss.tools import to_png
    except ImportError:
        notify("TTS Replay", "mss not installed")
        return

    if prefetched_monitor is not None:
        monitor = prefetched_monitor
    else:
        cfg = load_config()
        if "region" not in cfg:
            notify("TTS Replay", "No region set — calibrate first")
            return
        region = cfg["region"]
        if region.get("width", 0) < 10 or region.get("height", 0) < 10:
            notify("TTS Replay", "Capture region is too small — please recalibrate")
            return
        monitor = {k: region[k] for k in ("left", "top", "width", "height")}

    with _state_lock:
        if _state["manifest"] is None:
            session_stamp = datetime.now().strftime("session_%Y%m%d_%H%M%S")
            session_dir   = STORE_DIR / session_stamp
            session_dir.mkdir(parents=True, exist_ok=True)
            _state["session_dir"] = session_dir
            _state["frame_num"]   = 0
            _state["manifest"]    = {
                "session_id":    session_stamp,
                "session_start": datetime.now().isoformat(),
                "frames":        [],
            }
            with open(session_dir / "manifest.json", "w") as f:
                json.dump(_state["manifest"], f, indent=2)

        session_dir   = _state["session_dir"]
        manifest_path = session_dir / "manifest.json"
        ts            = datetime.now()
        turn          = _state["frame_num"] + 1

    filename = f"turn_{turn:04d}_{ts.strftime('%H%M%S')}.png"
    filepath = session_dir / filename

    with mss.mss() as sct:
        raw = sct.grab(monitor)
        to_png(raw.rgb, raw.size, output=str(filepath))

    stripped = strip_drawing_lines(filepath)

    with _state_lock:
        entry = {
            "filename":  filename,
            "timestamp": ts.isoformat(),
            "turn":      turn,
        }
        if scores:
            entry["scores"] = scores
        if cards:
            entry["cards"] = cards
        _state["manifest"]["frames"].append(entry)
        with open(manifest_path, "w") as f:
            json.dump(_state["manifest"], f, indent=2)
        _state["frame_num"] += 1

    tag = " (filtered)" if stripped else ""
    notify("TTS Replay", f"Turn {turn} captured{tag}")
    _update_ui()

# ── HTML export ───────────────────────────────────────────────────────────────

def compile_html(session_dir: Path) -> Path | None:
    """Encode all turn_*.png frames into a single self-contained HTML replay."""
    import base64

    frames = sorted(session_dir.glob("turn_*.png"))
    if not frames:
        return None

    manifest_path = session_dir / "manifest.json"
    manifest_frames = {}
    if manifest_path.exists():
        with open(manifest_path) as f:
            mdata = json.load(f)
        for entry in mdata.get("frames", []):
            manifest_frames[entry["filename"]] = entry

    slides           = []
    scores_per_frame = []
    cards_per_frame  = []
    timestamps       = []
    for fp in frames:
        data = base64.b64encode(fp.read_bytes()).decode()
        slides.append(f'data:image/png;base64,{data}')
        entry = manifest_frames.get(fp.name, {})
        scores_per_frame.append(entry.get("scores"))
        cards_per_frame.append(entry.get("cards"))
        timestamps.append(entry.get("timestamp", ""))

    slides_js = ",\n".join(f'"{s}"' for s in slides)
    scores_js = json.dumps(scores_per_frame)
    cards_js  = json.dumps(cards_per_frame)
    times_js  = json.dumps(timestamps)
    title     = session_dir.name

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Battle Replay \u2014 {title}</title>
<style>
  :root {{
    --red:    #e05555;
    --blue:   #5588e0;
    --bg:     #0d0f14;
    --panel:  #13161e;
    --card:   #1c2030;
    --border: #252840;
    --text:   #d0d6e8;
    --muted:  #5a6080;
    --accent: #f0c040;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text);
          font-family: "Segoe UI", system-ui, sans-serif;
          display: flex; flex-direction: column; align-items: center;
          min-height: 100vh; padding: 20px 16px; gap: 14px; }}
  .header {{ text-align: center; }}
  .header h1 {{ font-size: 1.3rem; letter-spacing: .08em;
                text-transform: uppercase; color: var(--accent); }}
  .header .sub {{ font-size: .75rem; color: var(--muted); margin-top: 2px; }}
  #viewer {{ max-width: 100%; max-height: 60vh;
             border: 2px solid var(--border); border-radius: 6px;
             display: block; box-shadow: 0 4px 24px #0008; }}
  .controls {{ display: flex; align-items: center; gap: 10px; }}
  .btn {{ background: var(--card); color: var(--text); border: 1px solid var(--border);
          padding: 6px 18px; cursor: pointer; border-radius: 4px; font-size: .9rem;
          transition: background .15s; }}
  .btn:hover {{ background: var(--border); }}
  input[type=range] {{ width: 240px; accent-color: var(--accent); }}
  #label {{ min-width: 90px; text-align: center; font-size: .85rem; color: var(--muted); }}
  #timestamp {{ font-size: .75rem; color: var(--muted); text-align: center; }}
  #dataPanel {{ width: 100%; max-width: 900px; display: none; flex-direction: column; gap: 12px; }}
  #dataPanel.visible {{ display: flex; }}
  .players {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .player-block {{ background: var(--panel); border: 1px solid var(--border);
                   border-radius: 8px; padding: 14px; }}
  .player-block.p1 {{ border-top: 3px solid var(--red); }}
  .player-block.p2 {{ border-top: 3px solid var(--blue); }}
  .player-name {{ font-size: 1rem; font-weight: 700; margin-bottom: 10px; }}
  .player-block.p1 .player-name {{ color: var(--red); }}
  .player-block.p2 .player-name {{ color: var(--blue); }}
  .player-label {{ font-size: .65rem; text-transform: uppercase;
                   letter-spacing: .1em; color: var(--muted); margin-bottom: 2px; }}
  .card-pill {{ background: var(--card); border: 1px solid var(--border);
                border-radius: 4px; padding: 5px 10px; font-size: .82rem;
                color: var(--text); margin-bottom: 6px; }}
  .card-pill.empty {{ color: var(--muted); font-style: italic; }}
  .player-total {{ font-size: 2rem; font-weight: 800; text-align: center;
                   margin-top: 10px; padding-top: 10px;
                   border-top: 1px solid var(--border); }}
  .player-block.p1 .player-total {{ color: var(--red); }}
  .player-block.p2 .player-total {{ color: var(--blue); }}
  .total-label {{ font-size: .7rem; color: var(--muted); text-align: center;
                  text-transform: uppercase; letter-spacing: .08em; }}
  .shared-cards {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }}
  .shared-card {{ background: var(--panel); border: 1px solid var(--border);
                  border-radius: 6px; padding: 10px 12px; }}
  .round-table-wrap {{ background: var(--panel); border: 1px solid var(--border);
                       border-radius: 8px; overflow: hidden; }}
  .round-table-wrap h3 {{ font-size: .7rem; text-transform: uppercase;
                          letter-spacing: .1em; color: var(--muted);
                          padding: 10px 14px 6px; }}
  table.rtable {{ width: 100%; border-collapse: collapse; font-size: .82rem; }}
  .rtable th {{ padding: 4px 8px; text-align: center; color: var(--muted);
                font-weight: 400; font-size: .72rem; text-transform: uppercase;
                letter-spacing: .06em; border-bottom: 1px solid var(--border); }}
  .rtable th.p1h {{ color: var(--red); }}
  .rtable th.p2h {{ color: var(--blue); }}
  .rtable td {{ padding: 5px 8px; text-align: center; border-bottom: 1px solid #1a1d28; }}
  .rtable tr:last-child td {{ border-bottom: none; }}
  .rtable td.rnd {{ color: var(--muted); font-size: .75rem; text-align: left; padding-left: 14px; }}
  .rtable td.tot {{ font-weight: 700; }}
  .rtable td.tot.p1 {{ color: var(--red); }}
  .rtable td.tot.p2 {{ color: var(--blue); }}
  .rtable tr.dim td {{ opacity: .3; }}
  .rtable tr.total-row td {{ border-top: 2px solid var(--border);
                              font-weight: 600; background: #0d0f1a; }}
  .divider {{ width: 1px; background: var(--border); }}
  .no-data {{ text-align: center; color: var(--muted); font-size: .85rem; padding: 16px; }}
</style>
</head>
<body>
<div class="header">
  <h1>Battle Replay</h1>
  <div class="sub">{title}</div>
</div>
<img id="viewer" src="">
<div class="controls">
  <button class="btn" id="prev">&#8592; Prev</button>
  <input type="range" id="slider" min="0" max="0" value="0">
  <button class="btn" id="next">Next &#8594;</button>
  <span id="label"></span>
</div>
<div id="timestamp"></div>
<div id="dataPanel"></div>
<script>
  const slides = [{slides_js}];
  const scores = {scores_js};
  const cards  = {cards_js};
  const times  = {times_js};

  const img     = document.getElementById('viewer');
  const slider  = document.getElementById('slider');
  const labelEl = document.getElementById('label');
  const tsEl    = document.getElementById('timestamp');
  const panel   = document.getElementById('dataPanel');
  let cur = 0;
  slider.max = slides.length - 1;

  function card(name) {{
    if (!name) return '<div class="card-pill empty">\u2014 none \u2014</div>';
    return `<div class="card-pill">${{name}}</div>`;
  }}

  function renderPanel(s, c) {{
    if (!s && !c) {{
      panel.className = 'visible';
      panel.innerHTML = '<div class="no-data">No score or card data for this turn</div>';
      return;
    }}
    const p1 = s ? s.red  : null;
    const p2 = s ? s.blue : null;
    const p1name = p1 ? p1.name : 'Player 1 (Red)';
    const p2name = p2 ? p2.name : 'Player 2 (Blue)';

    const p1Block = `
      <div class="player-block p1">
        <div class="player-name">\u25a0 ${{p1name}}</div>
        <div class="player-label">Secondary 1</div>${{card(c && c.p1_sec1)}}
        <div class="player-label">Secondary 2</div>${{card(c && c.p1_sec2)}}
        ${{p1 ? `<div class="player-total">${{p1.total}}</div>
          <div class="total-label">Total VP</div>
          <div style="margin-top:8px;font-size:.78rem;color:var(--muted)">
            PRI&nbsp;${{p1.primary}}&nbsp;&middot;&nbsp;SEC&nbsp;${{p1.secondary}}&nbsp;&middot;&nbsp;CHL&nbsp;${{p1.challenger}}&nbsp;&middot;&nbsp;Painted&nbsp;${{p1.painted}}
          </div>` : ''}}
      </div>`;

    const p2Block = `
      <div class="player-block p2">
        <div class="player-name">\u25a0 ${{p2name}}</div>
        <div class="player-label">Secondary 1</div>${{card(c && c.p2_sec1)}}
        <div class="player-label">Secondary 2</div>${{card(c && c.p2_sec2)}}
        ${{p2 ? `<div class="player-total">${{p2.total}}</div>
          <div class="total-label">Total VP</div>
          <div style="margin-top:8px;font-size:.78rem;color:var(--muted)">
            PRI&nbsp;${{p2.primary}}&nbsp;&middot;&nbsp;SEC&nbsp;${{p2.secondary}}&nbsp;&middot;&nbsp;CHL&nbsp;${{p2.challenger}}&nbsp;&middot;&nbsp;Painted&nbsp;${{p2.painted}}
          </div>` : ''}}
      </div>`;

    const sharedCards = c ? `
      <div class="shared-cards">
        <div class="shared-card"><div class="player-label">Deployment</div>${{card(c.deployment)}}</div>
        <div class="shared-card"><div class="player-label">Primary Mission</div>${{card(c.primary)}}</div>
        <div class="shared-card"><div class="player-label">Challenger Card</div>${{card(c.challenger)}}</div>
      </div>` : '';

    let roundRows = '';
    if (s) {{
      const rounds = Math.max(p1.rounds.length, p2.rounds.length);
      for (let r = 0; r < rounds; r++) {{
        const r1 = p1.rounds[r] || {{}}, r2 = p2.rounds[r] || {{}};
        const hasScore = (r1.total || 0) + (r2.total || 0) > 0;
        roundRows += `<tr class="${{hasScore ? '' : 'dim'}}">
          <td class="rnd">Round ${{r + 1}}</td>
          <td class="tot p1">${{r1.total ?? '-'}}</td>
          <td>${{r1.primary ?? '-'}}</td><td>${{r1.secondary ?? '-'}}</td><td>${{r1.challenger ?? '-'}}</td>
          <td class="divider"></td>
          <td>${{r2.challenger ?? '-'}}</td><td>${{r2.secondary ?? '-'}}</td><td>${{r2.primary ?? '-'}}</td>
          <td class="tot p2">${{r2.total ?? '-'}}</td>
        </tr>`;
      }}
      roundRows += `<tr class="total-row">
        <td class="rnd">Totals</td>
        <td class="tot p1">${{p1.total}}</td>
        <td>${{p1.primary}}</td><td>${{p1.secondary}}</td><td>${{p1.challenger}}</td>
        <td class="divider"></td>
        <td>${{p2.challenger}}</td><td>${{p2.secondary}}</td><td>${{p2.primary}}</td>
        <td class="tot p2">${{p2.total}}</td>
      </tr>`;
    }}

    const roundTable = s ? `
      <div class="round-table-wrap">
        <h3>Round by Round</h3>
        <table class="rtable">
          <thead><tr>
            <th></th>
            <th class="p1h" colspan="4">\u25a0 ${{p1name}} &nbsp; TOT &middot; PRI &middot; SEC &middot; CHL</th>
            <th></th>
            <th class="p2h" colspan="4">CHL &middot; SEC &middot; PRI &middot; TOT &nbsp; \u25a0 ${{p2name}}</th>
          </tr></thead>
          <tbody>${{roundRows}}</tbody>
        </table>
      </div>` : '';

    panel.className = 'visible';
    panel.innerHTML = `<div class="players">${{p1Block}}${{p2Block}}</div>${{sharedCards}}${{roundTable}}`;
  }}

  function fmt(iso) {{
    if (!iso) return '';
    try {{ return new Date(iso).toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}}); }}
    catch(e) {{ return iso; }}
  }}

  function show(n) {{
    cur = Math.max(0, Math.min(slides.length - 1, n));
    img.src             = slides[cur];
    slider.value        = cur;
    labelEl.textContent = `Turn ${{cur + 1}} / ${{slides.length}}`;
    tsEl.textContent    = times[cur] ? `Captured ${{fmt(times[cur])}}` : '';
    renderPanel(scores[cur], cards[cur]);
  }}

  document.getElementById('prev').onclick = () => show(cur - 1);
  document.getElementById('next').onclick = () => show(cur + 1);
  slider.oninput = () => show(+slider.value);
  document.addEventListener('keydown', e => {{
    if (e.key === 'ArrowLeft')  show(cur - 1);
    if (e.key === 'ArrowRight') show(cur + 1);
  }});
  show(0);
</script>
</body>
</html>"""

    out = session_dir / f"{session_dir.name}.html"
    out.write_text(html, encoding="utf-8")
    return out

# ── TTS TCP listener ───────────────────────────────────────────────────────────
# TTS owns port 39998 as the server. We connect as a client and keep the
# connection open. sendExternalMessage() in Lua pushes JSON to us; we can
# also send JSON back and TTS receives it via onExternalMessage().

def _dispatch_action(action: str,
                     scores: dict | None = None,
                     cards:  dict | None = None):
    if action == "capture":
        threading.Thread(target=_delayed_capture,
                         kwargs={"scores": scores, "cards": cards},
                         daemon=True).start()
    elif action == "capture_auto":
        threading.Thread(target=_delayed_capture,
                         kwargs={"skip_on_unstable": True,
                                 "scores": scores, "cards": cards},
                         daemon=True).start()

def _listener_thread():
    notify("TTS Replay", "Connecting to TTS\u2026")

    buf = ""
    while _state["listening"]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(TTS_RECONNECT_INTERVAL)
            sock.connect(("localhost", TTS_LISTEN_PORT))
            sock.settimeout(None)
        except (ConnectionRefusedError, socket.timeout, OSError):
            sock.close()
            for _ in range(TTS_RECONNECT_INTERVAL * 10):
                if not _state["listening"]:
                    return
                time.sleep(0.1)
            continue

        notify("TTS Replay", "Connected to TTS \u2014 waiting for capture signal")
        try:
            sock.sendall((json.dumps({"action": "handshake"}) + "\n").encode("utf-8"))
        except Exception:
            pass
        buf = ""

        try:
            while _state["listening"]:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="ignore")

                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if isinstance(msg, dict):
                            custom = msg.get("customMessage") or {}
                            action = custom.get("action")
                            scores = custom.get("scores")
                            cards  = custom.get("cards")
                            if action:
                                _dispatch_action(action, scores, cards)
                    except json.JSONDecodeError:
                        pass

        except Exception:
            pass
        finally:
            sock.close()

        if _state["listening"]:
            notify("TTS Replay", "TTS disconnected \u2014 reconnecting\u2026")

# ── Stability-polling capture ─────────────────────────────────────────────────

def _grab_region(monitor: dict):
    import mss
    with mss.mss() as sct:
        raw = sct.grab(monitor)
    return bytes(raw.rgb)

def _frames_stable(a: bytes, b: bytes) -> bool:
    if len(a) != len(b):
        return False
    total   = len(a) // 3
    matches = sum(1 for i in range(0, len(a), 3)
                  if a[i] == b[i] and a[i+1] == b[i+1] and a[i+2] == b[i+2])
    return (matches / total) >= STABILITY_THRESHOLD

def _delayed_capture(skip_on_unstable: bool = False,
                     scores: dict | None = None,
                     cards:  dict | None = None):
    cfg    = load_config()
    region = cfg.get("region")
    if not region or region.get("width", 0) < 10 or region.get("height", 0) < 10:
        return

    monitor = {k: region[k] for k in ("left", "top", "width", "height")}
    time.sleep(CAMERA_SETTLE_MS / 1000)

    prev  = _grab_region(monitor)
    polls = 0
    while polls < STABILITY_MAX_POLLS:
        time.sleep(STABILITY_POLL_MS / 1000)
        curr = _grab_region(monitor)
        if _frames_stable(prev, curr):
            break
        prev  = curr
        polls += 1

    if polls == STABILITY_MAX_POLLS:
        if skip_on_unstable:
            notify("TTS Replay", "\u26a0 Auto-capture skipped \u2014 camera still moving")
            return
        notify("TTS Replay", "\u26a0 Camera may not have settled \u2014 frame captured anyway")

    take_screenshot(prefetched_monitor=monitor, scores=scores, cards=cards)

# ── Session control ───────────────────────────────────────────────────────────

def start_listening():
    if _state["listening"]:
        return
    _state["listening"]   = True
    _state["manifest"]    = None
    _state["session_dir"] = None
    _state["frame_num"]   = 0
    threading.Thread(target=_listener_thread, daemon=True).start()
    _update_ui()

def stop_listening():
    if not _state["listening"]:
        return
    _state["listening"] = False
    _update_ui()

    session_dir = _state.get("session_dir")
    if session_dir and _state.get("frame_num", 0) > 0:
        def _compile():
            notify("TTS Replay", "Compiling replay\u2026")
            html_path = compile_html(session_dir)
            if html_path:
                notify("TTS Replay", f"Replay saved: {html_path.name}")
                os.startfile(str(session_dir))
            else:
                notify("TTS Replay", "No frames to compile")
        threading.Thread(target=_compile, daemon=True).start()
    else:
        notify("TTS Replay", "Session ended \u2014 no frames captured")

# ── Calibration ───────────────────────────────────────────────────────────────

def _run_calibrate():
    import tkinter as tk

    result = {}

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)
    root.configure(bg="black", cursor="crosshair")
    root.title("Drag to select battlefield region \u2014 Esc to cancel")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    s = {"start": None, "rect": None}

    def on_press(e):
        s["start"] = (e.x, e.y)
        if s["rect"]: canvas.delete(s["rect"])

    def on_drag(e):
        if s["start"]:
            x0, y0 = s["start"]
            if s["rect"]: canvas.delete(s["rect"])
            s["rect"] = canvas.create_rectangle(
                x0, y0, e.x, e.y,
                outline="#00ff88", width=3,
                fill="#00ff88", stipple="gray25"
            )

    def on_release(e):
        if s["start"] is None:
            return
        x0, y0 = s["start"]
        x1, y1 = e.x, e.y
        if abs(x1 - x0) < 10 or abs(y1 - y0) < 10:
            return
        result["region"] = {
            "left":   min(x0, x1), "top":    min(y0, y1),
            "width":  abs(x1 - x0), "height": abs(y1 - y0),
        }
        root.destroy()

    def on_escape(e): root.destroy()

    canvas.bind("<ButtonPress-1>",   on_press)
    canvas.bind("<B1-Motion>",       on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>",            on_escape)
    root.mainloop()

    if "region" in result:
        cfg = load_config()
        cfg["region"] = result["region"]
        save_config(cfg)
        _update_ui()
        notify("TTS Replay", "Region saved")
    else:
        notify("TTS Replay", "Calibration cancelled")

def do_calibrate():
    threading.Thread(target=_run_calibrate, daemon=True).start()

# ── Taskbar window ────────────────────────────────────────────────────────────

def _update_ui():
    win = _state.get("window")
    if not win or not win.winfo_exists():
        return
    win.after(0, _apply_ui_state)

def _apply_ui_state():
    listening  = _state["listening"]
    has_region = "region" in load_config()
    frame_num  = _state.get("frame_num", 0)

    if listening:
        _state["status_var"].set(
            f"\u25cf RECORDING  \u2014  {frame_num} frame{'s' if frame_num != 1 else ''} captured"
        )
        _state["indicator"].config(bg="#2dce6a")
        _state["btn_session"].config(
            text="\u25a0  Stop Session", bg="#c8391a",
            command=stop_listening
        )
    else:
        _state["indicator"].config(bg="#c8391a")
        _state["btn_session"].config(
            text="\u25b6  Start Session", bg="#2dce6a",
            command=start_listening,
            state="normal" if has_region else "disabled"
        )
        if not has_region:
            _state["status_var"].set("Not calibrated \u2014 use Calibrate Region first")
        elif frame_num == 0:
            _state["status_var"].set("Ready \u2014 start a session to begin recording")

def _exit_app():
    from tkinter import messagebox
    if _state["listening"] and _state.get("frame_num", 0) > 0:
        answer = messagebox.askyesnocancel(
            "TTS Replay \u2014 Exit?",
            f"You have {_state['frame_num']} captured frame(s) in an active session.\n\n"
            "Do you want to compile them into a replay before exiting?\n\n"
            "  Yes   \u2192 compile replay, then exit\n"
            "  No    \u2192 exit without saving\n"
            "  Cancel \u2192 go back",
            icon="warning",
        )
        if answer is True:
            stop_listening()
            time.sleep(3)
            _state["window"].destroy()
        elif answer is False:
            _state["listening"] = False
            _state["window"].destroy()
    else:
        _state["listening"] = False
        _state["window"].destroy()

def _build_window():
    import tkinter as tk

    win = tk.Tk()
    win.title("TTS Battlefield Replay")
    win.configure(bg="#1a1e26")
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", _exit_app)

    _state["window"]     = win
    _state["status_var"] = tk.StringVar(value="Initialising\u2026")

    top = tk.Frame(win, bg="#1a1e26")
    top.pack(fill="x", padx=12, pady=(12, 4))

    indicator = tk.Label(top, width=2, bg="#c8391a", relief="flat")
    indicator.pack(side="left", fill="y", padx=(0, 8))
    _state["indicator"] = indicator

    tk.Label(top, textvariable=_state["status_var"],
             bg="#1a1e26", fg="#c8d4e0",
             font=("Segoe UI", 9), anchor="w",
             wraplength=300, justify="left").pack(side="left", fill="x", expand=True)

    btn_frame = tk.Frame(win, bg="#1a1e26")
    btn_frame.pack(fill="x", padx=12, pady=(4, 12))

    btn_opts = dict(font=("Segoe UI", 10, "bold"), relief="flat",
                    padx=10, pady=6, cursor="hand2", fg="#ffffff", width=22)

    btn_session = tk.Button(btn_frame, text="\u25b6  Start Session",
                            bg="#2dce6a", **btn_opts)
    btn_session.pack(fill="x", pady=(0, 4))
    _state["btn_session"] = btn_session

    tk.Button(btn_frame, text="\U0001f3af  Calibrate Region",
              bg="#2a3040", command=do_calibrate, **btn_opts).pack(fill="x", pady=(0, 4))

    tk.Button(btn_frame, text="Exit",
              bg="#111418", command=_exit_app, **btn_opts).pack(fill="x")

    _apply_ui_state()
    return win

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_assets()
    win = _build_window()
    win.mainloop()
