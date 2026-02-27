"""
TTS Battlefield Replay — System Tray App
=========================================
Double-click to start. A tray icon appears in the Windows system tray.

Tray menu:
  • Calibrate Region   — drag to select battlefield area
  • Start Session      — begin listening for TTS capture signals
  • Stop Session       — stop listening
  • Open Replay        — open playback.html in browser
  • Clean Frames       — reprocess existing frames to strip drawing lines
  • Setup Instructions — show the TTS Lua setup guide
  • Exit

Requirements (for running from source):
  pip install mss Pillow pystray opencv-python-headless numpy
"""

import sys
import os
import json
import time
import socket
import threading
import webbrowser
import http.server
import socketserver
from datetime import datetime
from pathlib import Path

# ── Paths (dynamic — works from .py or compiled .exe) ─────────────────────────

def _app_dir() -> Path:
    """Directory containing the exe (or the .py script when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

APP_DIR    = _app_dir()
STORE_DIR  = APP_DIR / "TTS Replay Sessions"
CONFIG_FILE = APP_DIR / "config.json"

SERVER_PORT      = 8080
TTS_LISTEN_PORT  = 39998
CAMERA_SETTLE_MS = 500

# ── Embedded assets ───────────────────────────────────────────────────────────

PLAYBACK_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BATTLE REPLAY — Glory Hogs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Rajdhani:wght@400;500;600;700&family=Bebas+Neue&display=swap" rel="stylesheet">

<style>
  :root {
    --bg:        #0a0c0f;
    --surface:   #111418;
    --border:    #1e2530;
    --accent:    #c8391a;
    --accent2:   #e8a020;
    --green:     #2dce6a;
    --dim:       #3a4455;
    --text:      #c8d4e0;
    --text-dim:  #5a6878;
    --scan:      rgba(200, 57, 26, 0.04);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    overflow: hidden;
  }

  body::after {
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      var(--scan) 2px, var(--scan) 4px
    );
    pointer-events: none;
    z-index: 1000;
  }

  .app {
    display: grid;
    grid-template-rows: 56px 1fr 140px;
    height: 100vh;
    gap: 0;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: relative;
    overflow: hidden;
  }
  header::before {
    content: '';
    position: absolute; left: 0; top: 0; bottom: 0;
    width: 4px;
    background: var(--accent);
  }

  .logo {
    font-family: 'Bebas Neue', sans-serif;
    font-size: 28px;
    letter-spacing: 4px;
    color: var(--accent);
    text-shadow: 0 0 20px rgba(200,57,26,0.5);
  }
  .logo span { color: var(--text-dim); font-size: 16px; letter-spacing: 2px; margin-left: 12px; }

  .header-meta {
    display: flex;
    gap: 32px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 12px;
    color: var(--text-dim);
  }
  .header-meta .val { color: var(--accent2); }

  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    display: inline-block;
    margin-right: 6px;
    box-shadow: 0 0 8px var(--green);
  }
  .status-dot.inactive { background: var(--dim); box-shadow: none; }

  .viewer {
    position: relative;
    overflow: hidden;
    background: #050607;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  #battlefield-img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    display: block;
    transition: opacity 0.15s ease;
    image-rendering: pixelated;
  }

  .corner {
    position: absolute;
    width: 24px; height: 24px;
    pointer-events: none;
    opacity: 0.6;
  }
  .corner::before, .corner::after {
    content: '';
    position: absolute;
    background: var(--accent);
  }
  .corner::before { width: 100%; height: 2px; top: 0; left: 0; }
  .corner::after  { width: 2px; height: 100%; top: 0; left: 0; }
  .corner.tl { top: 16px; left: 16px; }
  .corner.tr { top: 16px; right: 16px; transform: scaleX(-1); }
  .corner.bl { bottom: 16px; left: 16px; transform: scaleY(-1); }
  .corner.br { bottom: 16px; right: 16px; transform: scale(-1); }

  .ts-overlay {
    position: absolute;
    bottom: 20px; right: 20px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 13px;
    color: rgba(200, 212, 224, 0.7);
    background: rgba(0,0,0,0.6);
    padding: 4px 10px;
    border-left: 2px solid var(--accent);
    letter-spacing: 1px;
  }

  .frame-counter {
    position: absolute;
    top: 20px; left: 20px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 11px;
    color: var(--accent);
    background: rgba(0,0,0,0.6);
    padding: 4px 10px;
    letter-spacing: 1px;
    border: 1px solid rgba(200,57,26,0.3);
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    color: var(--text-dim);
    font-family: 'Share Tech Mono', monospace;
    font-size: 13px;
    letter-spacing: 1px;
  }
  .empty-state .big { font-family: 'Bebas Neue', sans-serif; font-size: 48px; color: var(--dim); letter-spacing: 6px; }

  .controls {
    background: var(--surface);
    border-top: 1px solid var(--border);
    display: grid;
    grid-template-rows: auto 1fr;
    padding: 0;
    overflow: hidden;
  }

  .timeline-wrap {
    padding: 10px 24px 6px;
    position: relative;
  }

  .timeline-track {
    position: relative;
    height: 32px;
    cursor: pointer;
    user-select: none;
  }

  .thumb-strip {
    display: flex;
    gap: 3px;
    height: 100%;
    overflow: hidden;
    border: 1px solid var(--border);
    background: var(--bg);
  }

  .thumb {
    flex-shrink: 0;
    height: 100%;
    background: var(--dim);
    cursor: pointer;
    transition: opacity 0.1s;
    object-fit: cover;
    opacity: 0.6;
  }
  .thumb:hover { opacity: 1; }
  .thumb.active { opacity: 1; outline: 2px solid var(--accent); outline-offset: -2px; }

  #playhead {
    position: absolute;
    top: 0; bottom: 0;
    width: 2px;
    background: var(--accent);
    box-shadow: 0 0 8px var(--accent);
    pointer-events: none;
    transition: left 0.1s linear;
  }
  #playhead::after {
    content: '';
    position: absolute;
    top: -4px; left: 50%;
    transform: translateX(-50%);
    border: 5px solid transparent;
    border-top-color: var(--accent);
  }

  .ctrl-bar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 24px 10px;
  }

  .btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--text);
    font-family: 'Rajdhani', sans-serif;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 1px;
    padding: 6px 16px;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s, background 0.15s;
    text-transform: uppercase;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    min-width: 90px;
  }
  .btn.primary:hover { background: #e04020; }

  .btn-icon {
    width: 36px; height: 36px;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px;
    padding: 0;
  }

  .speed-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 12px;
    color: var(--text-dim);
    margin-left: auto;
  }
  .speed-btn { padding: 4px 10px; font-size: 12px; }
  .speed-btn.active { border-color: var(--accent2); color: var(--accent2); }

  .time-display {
    font-family: 'Share Tech Mono', monospace;
    font-size: 13px;
    color: var(--accent2);
    letter-spacing: 1px;
    white-space: nowrap;
    min-width: 120px;
  }

  .session-info {
    font-family: 'Share Tech Mono', monospace;
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 1px;
  }

  .loading {
    font-family: 'Share Tech Mono', monospace;
    font-size: 12px;
    color: var(--accent2);
    letter-spacing: 2px;
    animation: blink 1s step-end infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
</style>
</head>
<body>
<div class="app">

  <header>
    <div class="logo">
      BATTLE REPLAY
      <span>// GLORY HOGS</span>
    </div>
    <div class="header-meta">
      <div><span class="status-dot" id="status-dot"></span><span id="status-text">NO SESSION</span></div>
      <div>FRAMES&nbsp;<span class="val" id="hdr-frames">—</span></div>
      <div>DURATION&nbsp;<span class="val" id="hdr-duration">—</span></div>
      <div>SESSION&nbsp;<span class="val" id="hdr-session">—</span></div>
    </div>
  </header>

  <div class="viewer" id="viewer">
    <div class="corner tl"></div>
    <div class="corner tr"></div>
    <div class="corner bl"></div>
    <div class="corner br"></div>

    <div class="empty-state" id="empty-state">
      <div class="big">NO DATA</div>
      <div>Load a session to begin replay</div>
      <div style="color:var(--dim)">Waiting for TTS Replay tray app...</div>
    </div>

    <img id="battlefield-img" src="" alt="" style="display:none">
    <div class="ts-overlay" id="ts-overlay" style="display:none"></div>
    <div class="frame-counter" id="frame-counter" style="display:none"></div>
  </div>

  <div class="controls">
    <div class="timeline-wrap">
      <div class="timeline-track" id="timeline-track">
        <div class="thumb-strip" id="thumb-strip"></div>
        <div id="playhead" style="left: 0; display: none;"></div>
      </div>
    </div>

    <div class="ctrl-bar">
      <button class="btn btn-icon" id="btn-prev" title="Previous frame">&#9664;&#9664;</button>
      <button class="btn primary btn-icon" id="btn-play" title="Play/Pause">&#9654;</button>
      <button class="btn btn-icon" id="btn-next" title="Next frame">&#9654;&#9654;</button>

      <div class="time-display" id="time-display">--:-- / --:--</div>
      <div class="session-info" id="session-info"></div>

      <div class="speed-wrap">
        SPEED&nbsp;
        <button class="btn speed-btn" data-speed="0.5">0.5&times;</button>
        <button class="btn speed-btn active" data-speed="1">1&times;</button>
        <button class="btn speed-btn" data-speed="2">2&times;</button>
        <button class="btn speed-btn" data-speed="4">4&times;</button>
      </div>

      <button class="btn" id="btn-load">LOAD SESSION</button>
    </div>
  </div>

</div>

<input type="file" id="file-input" accept=".json" style="display:none">

<script>
const state = {
  frames: [], currentIndex: 0, playing: false,
  speed: 1, playTimer: null, sessionStart: null,
};

const img          = document.getElementById('battlefield-img');
const emptyState   = document.getElementById('empty-state');
const tsOverlay    = document.getElementById('ts-overlay');
const frameCounter = document.getElementById('frame-counter');
const thumbStrip   = document.getElementById('thumb-strip');
const playhead     = document.getElementById('playhead');
const timeDisplay  = document.getElementById('time-display');
const sessionInfo  = document.getElementById('session-info');
const btnPlay      = document.getElementById('btn-play');
const btnPrev      = document.getElementById('btn-prev');
const btnNext      = document.getElementById('btn-next');
const btnLoad      = document.getElementById('btn-load');
const fileInput    = document.getElementById('file-input');
const hdrFrames    = document.getElementById('hdr-frames');
const hdrDuration  = document.getElementById('hdr-duration');
const hdrSession   = document.getElementById('hdr-session');
const statusDot    = document.getElementById('status-dot');
const statusText   = document.getElementById('status-text');

async function tryAutoLoad() {
  try {
    const latestRes = await fetch('latest.json?t=' + Date.now());
    if (!latestRes.ok) { showStatus('NO SESSION YET — PRESS CAPTURE IN TTS', false); return; }
    const latest = await latestRes.json();
    const sessionDir = latest.session_dir;
    const res = await fetch(sessionDir + '/manifest.json?t=' + Date.now());
    if (!res.ok) { showStatus('MANIFEST NOT FOUND: ' + sessionDir, false); return; }
    const manifest = await res.json();
    loadManifest(manifest, sessionDir + '/');
  } catch(e) {
    showStatus('ERROR — OPEN VIA http://localhost:8080/playback.html', false);
  }
}

function loadManifest(manifest, basePath) {
  if (!manifest.frames || manifest.frames.length === 0) { showStatus('EMPTY SESSION', false); return; }
  state.frames = manifest.frames.map(f => ({ ...f, src: basePath + f.filename }));
  state.sessionStart = manifest.session_start;
  state.currentIndex = 0;
  buildThumbs();
  updateHeader();
  showFrame(0);
  showStatus('SESSION LOADED', true);
}

function buildThumbs() {
  thumbStrip.innerHTML = '';
  const trackWidth = document.getElementById('timeline-track').offsetWidth;
  const thumbW = Math.max(4, Math.floor(trackWidth / state.frames.length) - 3);
  state.frames.forEach((f, i) => {
    const el = document.createElement('img');
    el.className = 'thumb';
    el.src = f.src;
    el.style.width = thumbW + 'px';
    el.style.minWidth = thumbW + 'px';
    el.addEventListener('click', () => { stopPlay(); showFrame(i); });
    el.dataset.index = i;
    thumbStrip.appendChild(el);
  });
  playhead.style.display = 'block';
  updatePlayhead();
}

function updateThumbHighlight() {
  thumbStrip.querySelectorAll('.thumb').forEach(t => {
    t.classList.toggle('active', +t.dataset.index === state.currentIndex);
  });
}

function updatePlayhead() {
  if (state.frames.length === 0) return;
  const track = document.getElementById('timeline-track');
  const pct = state.frames.length > 1 ? state.currentIndex / (state.frames.length - 1) : 0;
  playhead.style.left = (pct * track.offsetWidth) + 'px';
}

function showFrame(index) {
  if (state.frames.length === 0) return;
  index = Math.max(0, Math.min(index, state.frames.length - 1));
  state.currentIndex = index;
  const f = state.frames[index];
  emptyState.style.display = 'none';
  img.style.display = 'block';
  img.style.opacity = '0.4';
  img.src = f.src;
  img.onload = () => { img.style.opacity = '1'; };
  tsOverlay.style.display = 'block';
  frameCounter.style.display = 'block';
  const d = new Date(f.timestamp);
  tsOverlay.textContent = d.toLocaleTimeString('en-GB');
  const elapsed = f.elapsed_seconds || (index * 60);
  const em = String(Math.floor(elapsed / 60)).padStart(2, '0');
  const es = String(elapsed % 60).padStart(2, '0');
  const totalSec = (state.frames.length - 1) * 60;
  const tm = String(Math.floor(totalSec / 60)).padStart(2, '0');
  const ts2 = String(totalSec % 60).padStart(2, '0');
  frameCounter.textContent = `FRAME ${String(index + 1).padStart(3,'0')} / ${String(state.frames.length).padStart(3,'0')}`;
  timeDisplay.textContent = `${em}:${es} / ${tm}:${ts2}`;
  updateThumbHighlight();
  updatePlayhead();
  const thumb = thumbStrip.querySelector(`.thumb[data-index="${index}"]`);
  if (thumb) thumb.scrollIntoView({ inline: 'nearest', behavior: 'smooth' });
}

function startPlay() {
  if (state.frames.length === 0) return;
  state.playing = true;
  btnPlay.textContent = '\u23F8';
  scheduleNext();
}

function scheduleNext() {
  if (!state.playing) return;
  state.playTimer = setTimeout(() => {
    if (state.currentIndex >= state.frames.length - 1) { stopPlay(); return; }
    showFrame(state.currentIndex + 1);
    scheduleNext();
  }, 1000 / state.speed);
}

function stopPlay() {
  state.playing = false;
  btnPlay.textContent = '\u25B6';
  clearTimeout(state.playTimer);
}

function togglePlay() { state.playing ? stopPlay() : startPlay(); }

function updateHeader() {
  hdrFrames.textContent = state.frames.length;
  const totalSec = (state.frames.length - 1) * 60;
  const hh = String(Math.floor(totalSec / 3600)).padStart(2,'0');
  const mm = String(Math.floor((totalSec % 3600) / 60)).padStart(2,'0');
  const ss = String(totalSec % 60).padStart(2,'0');
  hdrDuration.textContent = `${hh}:${mm}:${ss}`;
  if (state.sessionStart) {
    const d = new Date(state.sessionStart);
    hdrSession.textContent = d.toLocaleDateString('en-GB') + ' ' + d.toLocaleTimeString('en-GB');
  }
}

function showStatus(msg, active) {
  statusText.textContent = msg;
  statusDot.classList.toggle('inactive', !active);
}

btnPlay.addEventListener('click', togglePlay);
btnPrev.addEventListener('click', () => { stopPlay(); showFrame(state.currentIndex - 1); });
btnNext.addEventListener('click', () => { stopPlay(); showFrame(state.currentIndex + 1); });

document.querySelectorAll('.speed-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    state.speed = parseFloat(btn.dataset.speed);
    document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    if (state.playing) { clearTimeout(state.playTimer); scheduleNext(); }
  });
});

const track = document.getElementById('timeline-track');
function seekFromEvent(e) {
  const rect = track.getBoundingClientRect();
  const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  showFrame(Math.round(pct * (state.frames.length - 1)));
}
let dragging = false;
track.addEventListener('mousedown', e => { dragging = true; stopPlay(); seekFromEvent(e); });
window.addEventListener('mousemove', e => { if (dragging) seekFromEvent(e); });
window.addEventListener('mouseup',   () => { dragging = false; });

document.addEventListener('keydown', e => {
  if (e.key === ' ')          { e.preventDefault(); togglePlay(); }
  if (e.key === 'ArrowLeft')  { stopPlay(); showFrame(state.currentIndex - 1); }
  if (e.key === 'ArrowRight') { stopPlay(); showFrame(state.currentIndex + 1); }
  if (e.key === 'Home')       { stopPlay(); showFrame(0); }
  if (e.key === 'End')        { stopPlay(); showFrame(state.frames.length - 1); }
});

btnLoad.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
  if (!fileInput.files.length) return;
  const text = await fileInput.files[0].text();
  loadManifest(JSON.parse(text), 'screenshots/');
  fileInput.value = '';
});

tryAutoLoad();
// Auto-refresh every 10s to pick up new frames during a live session
setInterval(tryAutoLoad, 10000);
</script>
</body>
</html>
"""

CAPTURE_BUTTON_LUA = r"""-- TTS Battlefield Replay — Capture Button
-- Attach this script to any object in your TTS save.
-- Right-click object → Scripting → paste → Save & Play.
--
-- Set TOP_DOWN_POSITION to the centre of your battlefield (X, Y, Z).
-- Set TOP_DOWN_DISTANCE to control zoom — increase for larger boards.

local TOP_DOWN_POSITION = {
    x = 0,    -- centre of battlefield (X axis)
    y = 10,   -- height above table
    z = 0,    -- centre of battlefield (Z axis)
}

local TOP_DOWN_DISTANCE = 40

-- ─────────────────────────────────────────────────────────────────────────────

function onLoad()
    self.setScale({0.3, 0.3, 0.3})

    self.createButton({
        label          = "📷 CAPTURE",
        click_function = "doCapture",
        function_owner = self,
        position       = {0, 0.5, 0},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 300,
        font_size      = 120,
        color          = {0.1, 0.1, 0.1},
        font_color     = {1, 0.85, 0.2},
        tooltip        = "Capture this turn",
    })
end

function doCapture(obj, player_color, alt_click)
    -- Use whoever pressed the button as the camera to snap
    local player = Player[player_color]

    player.setCameraMode("TopDown")
    player.lookAt({
        position = TOP_DOWN_POSITION,
        pitch    = 90,
        yaw      = 0,
        distance = TOP_DOWN_DISTANCE,
    })

    -- Wait 1.0s for camera to settle, then signal Python
    -- Python adds another 500ms on top before grabbing the screenshot
    Wait.time(function()
        sendExternalMessage({ action = "capture" })
        Wait.time(function()
            player.setCameraMode("ThirdPerson")
        end, 0.8)
    end, 1.0)
end
"""

SETUP_INSTRUCTIONS = """TTS BATTLEFIELD REPLAY — SETUP INSTRUCTIONS
============================================

The tray app is now running. You only need to do the TTS setup once.

STEP 1 — Add the Lua capture button to TTS
  1. In Tabletop Simulator, right-click any small object (e.g. a coin or token)
  2. Choose Scripting → open the Lua editor
  3. Paste the contents of capture_button.lua (in the same folder as this app)
  4. Click Save & Play
  A 📷 CAPTURE button will appear on the object.

STEP 2 — Enable TTS External Editor API
  In TTS menu: Configuration → External Editor API
  Make sure it is ENABLED (port 39998).

STEP 3 — Calibrate the region
  Right-click the tray icon → Calibrate Region
  Drag over your battlefield area on screen.

STEP 4 — Start a session
  Right-click the tray icon → Start Session

STEP 5 — Capture turns
  Press the 📷 CAPTURE button in TTS at the end of each turn.
  The app will snap a screenshot automatically.

STEP 6 — View the replay
  Right-click the tray icon → Open Replay in Browser
  Use Edge or whitelist localhost in your ad blocker.

NOTE: capture_button.lua is in the same folder as this app.
      You can open it in Notepad to copy its contents.
"""

# ── TTS Drawing-line colours (HSV ranges) ────────────────────────────────────
DRAWING_COLORS = [
    ("red_lo",   0,   8, 200, 150),
    ("red_hi", 168, 179, 200, 150),
    ("teal",    80, 100, 150,  80),
    ("green",   50,  80, 200,  80),
    ("blue",   100, 115, 180, 150),
    ("purple", 130, 168, 150,  80),
    ("yellow",  20,  35, 220, 150),
]
STRIP_WHITE_LINES = True
INPAINT_RADIUS    = 5

# ── Shared app state ──────────────────────────────────────────────────────────
_state = {
    "listening":   False,
    "frame_num":   0,
    "manifest":    None,
    "session_dir": None,
    "tray":        None,
    "listener":    None,
    "server":      None,
    "httpd":       None,
}

# ── First-run setup ───────────────────────────────────────────────────────────

def ensure_assets():
    """Write playback.html and capture_button.lua on first run if missing."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)

    html_path = STORE_DIR / "playback.html"
    if not html_path.exists():
        html_path.write_text(PLAYBACK_HTML, encoding="utf-8")

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
    if _state["tray"]:
        _state["tray"].notify(msg, title)

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

    img_hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    h, w    = img_bgr.shape[:2]
    mask    = np.zeros((h, w), dtype=np.uint8)

    for (label, h_lo, h_hi, s_min, v_min) in DRAWING_COLORS:
        lo = np.array([h_lo, s_min, v_min])
        hi = np.array([h_hi, 255,   255  ])
        mask |= cv2.inRange(img_hsv, lo, hi)

    if STRIP_WHITE_LINES:
        mask |= cv2.inRange(img_hsv,
                            np.array([0,   0, 220]),
                            np.array([179, 30, 255]))

    if int(np.count_nonzero(mask)) == 0:
        return False

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask   = cv2.dilate(mask, kernel, iterations=2)
    result = cv2.inpaint(img_bgr, mask, INPAINT_RADIUS, cv2.INPAINT_TELEA)
    cv2.imwrite(str(filepath), result)
    return True

# ── Screenshot ────────────────────────────────────────────────────────────────

def take_screenshot():
    try:
        import mss
        from mss.tools import to_png
    except ImportError:
        notify("TTS Replay", "mss not installed")
        return

    cfg = load_config()
    if "region" not in cfg:
        notify("TTS Replay", "No region set — calibrate first")
        return

    monitor = {k: cfg["region"][k] for k in ("left", "top", "width", "height")}

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

    ts       = datetime.now()
    turn     = _state["frame_num"] + 1
    filename = f"turn_{turn:04d}_{ts.strftime('%H%M%S')}.png"
    filepath = session_dir / filename

    with mss.mss() as sct:
        raw = sct.grab(monitor)
        to_png(raw.rgb, raw.size, output=str(filepath))

    stripped = strip_drawing_lines(filepath)

    _state["manifest"]["frames"].append({
        "filename":  filename,
        "timestamp": ts.isoformat(),
        "turn":      turn,
    })
    with open(manifest_path, "w") as f:
        json.dump(_state["manifest"], f, indent=2)

    _state["frame_num"] += 1

    latest = {
        "session_dir": session_dir.name,
        "manifest":    session_dir.name + "/manifest.json",
    }
    with open(STORE_DIR / "latest.json", "w") as f:
        json.dump(latest, f, indent=2)

    tag = " (filtered)" if stripped else ""
    notify("TTS Replay", f"Turn {turn} captured{tag}")

# ── TTS TCP listener ──────────────────────────────────────────────────────────

def _listener_thread():
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("localhost", TTS_LISTEN_PORT))
        srv.listen(5)
        srv.settimeout(1.0)
    except OSError as e:
        notify("TTS Replay", f"Cannot bind port {TTS_LISTEN_PORT}: {e}")
        _state["listening"] = False
        _update_tray_menu()
        return

    notify("TTS Replay", "Ready — waiting for TTS capture signal")

    while _state["listening"]:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break

        try:
            data = b""
            conn.settimeout(2.0)
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        except Exception:
            pass
        finally:
            conn.close()

        if not data:
            continue

        try:
            msg    = json.loads(data.decode("utf-8", errors="ignore"))
            action = None
            if isinstance(msg, dict):
                action = msg.get("action") or \
                         (msg.get("customMessage") or {}).get("action")
            if action == "capture":
                threading.Thread(target=_delayed_capture, daemon=True).start()
        except Exception:
            pass

    srv.close()

def _delayed_capture():
    time.sleep(CAMERA_SETTLE_MS / 1000)
    take_screenshot()

def start_listening():
    if _state["listening"]:
        return
    _state["listening"]   = True
    _state["manifest"]    = None
    _state["session_dir"] = None
    _state["frame_num"]   = 0
    threading.Thread(target=_listener_thread, daemon=True).start()
    _update_tray_menu()

def stop_listening():
    if not _state["listening"]:
        return
    _state["listening"] = False
    _update_tray_menu()
    notify("TTS Replay", "Session stopped")

# ── HTTP server ───────────────────────────────────────────────────────────────

def _server_thread():
    os.chdir(STORE_DIR)

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass

    httpd = socketserver.TCPServer(("", SERVER_PORT), QuietHandler)
    _state["httpd"] = httpd
    httpd.serve_forever()

def ensure_server():
    if _state["server"] and _state["server"].is_alive():
        return
    t = threading.Thread(target=_server_thread, daemon=True)
    t.start()
    _state["server"] = t

# ── Calibration ───────────────────────────────────────────────────────────────

def _run_calibrate():
    import tkinter as tk

    result = {}
    root   = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)
    root.configure(bg="black")

    canvas = tk.Canvas(root, bg="black", cursor="crosshair", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        root.winfo_screenwidth() // 2, 40,
        text="Drag over the BATTLEFIELD area  |  ESC to cancel",
        fill="white", font=("Consolas", 18, "bold")
    )

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
        x0, y0 = s["start"]
        x1, y1 = e.x, e.y
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
        notify("TTS Replay", "Region saved ✓  —  you can now Start Session")
    else:
        notify("TTS Replay", "Calibration cancelled")

def do_calibrate():
    threading.Thread(target=_run_calibrate, daemon=True).start()

# ── Show setup instructions ───────────────────────────────────────────────────

def do_show_instructions():
    import tkinter as tk
    from tkinter import scrolledtext

    def _run():
        root = tk.Tk()
        root.title("TTS Replay — Setup Instructions")
        root.geometry("620x500")
        root.resizable(True, True)
        root.configure(bg="#0a0c0f")

        txt = scrolledtext.ScrolledText(
            root, wrap=tk.WORD, font=("Consolas", 10),
            bg="#0a0c0f", fg="#c8d4e0", insertbackground="white",
            relief="flat", padx=16, pady=16
        )
        txt.pack(fill="both", expand=True)
        txt.insert(tk.END, SETUP_INSTRUCTIONS)
        txt.configure(state="disabled")

        btn = tk.Button(
            root, text="Open capture_button.lua",
            command=lambda: os.startfile(str(APP_DIR / "capture_button.lua")),
            bg="#c8391a", fg="white", relief="flat",
            font=("Consolas", 10, "bold"), pady=8
        )
        btn.pack(fill="x", padx=16, pady=(0, 16))

        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()

# ── Clean existing frames ─────────────────────────────────────────────────────

def do_clean():
    def _run():
        search_dir = _state.get("session_dir") or STORE_DIR
        frames     = sorted(search_dir.glob("turn_*.png")) or sorted(search_dir.glob("frame_*.png"))
        if not frames:
            notify("TTS Replay", "No frames found to clean")
            return
        changed = sum(1 for fp in frames if strip_drawing_lines(fp))
        notify("TTS Replay", f"Clean done: {changed}/{len(frames)} frames modified")
    threading.Thread(target=_run, daemon=True).start()

# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_icon():
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60],   outline="#c8391a", width=5)
    draw.ellipse([20, 20, 44, 44], fill="#c8391a")
    return img

def _build_menu():
    import pystray
    listening  = _state["listening"]
    has_region = "region" in load_config()

    return pystray.Menu(
        pystray.MenuItem(
            "● ACTIVE — waiting for TTS" if listening else "○ Stopped",
            None, enabled=False
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Calibrate Region",     lambda icon, item: do_calibrate()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Stop Session" if listening else "Start Session",
            lambda icon, item: stop_listening() if _state["listening"] else start_listening(),
            enabled=has_region
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Replay in Browser", lambda icon, item: _open_replay()),
        pystray.MenuItem("Clean Existing Frames",  lambda icon, item: do_clean()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Setup Instructions",     lambda icon, item: do_show_instructions()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit",                   lambda icon, item: _exit_app()),
    )

def _update_tray_menu():
    if _state["tray"]:
        _state["tray"].menu = _build_menu()

def _open_replay():
    ensure_server()
    time.sleep(0.4)
    webbrowser.open(f"http://localhost:{SERVER_PORT}/playback.html")

def _exit_app():
    _state["listening"] = False
    if _state.get("httpd"):
        threading.Thread(target=_state["httpd"].shutdown, daemon=True).start()
    _state["tray"].stop()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        import pystray
        from PIL import Image
    except ImportError:
        print("Missing dependencies. Run:\n  pip install pystray pillow mss opencv-python-headless numpy")
        sys.exit(1)

    ensure_assets()

    icon = pystray.Icon(
        name  = "TTS Replay",
        icon  = _make_icon(),
        title = "TTS Battlefield Replay",
        menu  = _build_menu(),
    )
    _state["tray"] = icon
    ensure_server()
    icon.run()
