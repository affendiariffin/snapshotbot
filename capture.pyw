"""
TTS Battlefield Replay — System Tray App
=========================================
Double-click to start. A tray icon appears in the Windows system tray.

A small window sits on the taskbar with session controls.

Requirements (for running from source):
  pip install mss Pillow opencv-python-headless numpy
"""

import sys
import os
import json
import time
import socket
import threading
from datetime import datetime
from pathlib import Path

# ── Thread safety ────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()

# ── Paths (dynamic — works from .py or compiled .exe) ─────────────────────────

def _app_dir() -> Path:
    """Directory containing the exe (or the .py script when run from source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

APP_DIR    = _app_dir()
STORE_DIR  = APP_DIR / "TTS Replay Sessions"
CONFIG_FILE = APP_DIR / "config.json"

# How long to wait after the TTS signal before the first sample (ms)
TTS_LISTEN_PORT  = 39998   # TTS External Editor API port — TTS owns this as server
CAMERA_SETTLE_MS   = 500
# How long to wait between stability samples (ms)
STABILITY_POLL_MS  = 150
# Maximum number of samples before giving up and using whatever we have
STABILITY_MAX_POLLS = 8
# Fraction of pixels that must be identical between two samples to call it stable.
# 0.995 = 99.5% match — allows for minor HUD flicker without looping forever.
STABILITY_THRESHOLD = 0.995

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

-- ─────────────────────────────────────────────────────────────────────────────

-- Button indices (TTS assigns them in creation order, 0-based)
local BTN_CAPTURE = 0
local BTN_REC     = 1

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
        sendExternalMessage({ action = action_name })
        Wait.time(function()
            player.setCameraMode("ThirdPerson")
            capturing = false
        end, 0.8)
    end, 1.0)
end

-- ─────────────────────────────────────────────────────────────────────────────

function doCapture(obj, player_color, alt_click)
    -- Blocked while auto-rec is mid-sequence to avoid camera conflicts
    if not capturing then
        runSequence(player_color, "capture")
    end
end

-- ─────────────────────────────────────────────────────────────────────────────

function doToggleRec(obj, player_color, alt_click)
    if recording then
        -- Stop recording
        recording = false
        self.editButton({
            index      = BTN_REC,
            label      = "⏺ START REC",
            font_color = {0.9, 0.2, 0.2},
        })
    else
        -- Start recording
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
"""


# ── Drawing-line exact RGB values ────────────────────────────────────────────────
# Each entry is (label, (R, G, B)).
# Replace placeholder values with your exact TTS RGB readings.
DRAWING_COLORS_RGB = [
    ("red",    (218,  22,  22)),
    ("blue",   ( 28, 135, 255)),
    ("teal",   ( 34, 177, 155)),
    ("purple", (255,   0, 255)),
]

# Maximum per-channel deviation allowed (accounts for PNG compression rounding).
# 0 = pixel-perfect. Raise to ~8 if you see missed pixels on line edges.
RGB_TOLERANCE = 4

INPAINT_RADIUS = 5
# ── Shared app state ──────────────────────────────────────────────────────────
_state = {
    "listening":   False,
    "frame_num":   0,
    "manifest":    None,
    "session_dir": None,
    "tray":        None,
}

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

    # Work in RGB so the tuples above match directly
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.int16)
    h, w    = img_bgr.shape[:2]
    mask    = np.zeros((h, w), dtype=np.uint8)

    for label, (r, g, b) in DRAWING_COLORS_RGB:
        target = np.array([r, g, b], dtype=np.int16)
        diff   = np.abs(img_rgb - target)          # shape (h, w, 3)
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

def take_screenshot(prefetched_monitor: dict | None = None):
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
        _state["manifest"]["frames"].append({
            "filename":  filename,
            "timestamp": ts.isoformat(),
            "turn":      turn,
        })
        with open(manifest_path, "w") as f:
            json.dump(_state["manifest"], f, indent=2)
        _state["frame_num"] += 1

    tag = " (filtered)" if stripped else ""
    notify("TTS Replay", f"Turn {turn} captured{tag}")
    _update_ui()   # keep frame counter in tray status line current

# ── Video export ─────────────────────────────────────────────────────────────

def compile_html(session_dir: Path) -> Path | None:
    """Encode all turn_*.png frames into a single self-contained HTML replay."""
    import base64

    frames = sorted(session_dir.glob("turn_*.png"))
    if not frames:
        return None

    slides = []
    for fp in frames:
        data = base64.b64encode(fp.read_bytes()).decode()
        slides.append(f'data:image/png;base64,{data}')

    slides_js = ",\n".join(f'"{s}"' for s in slides)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Battle Replay — {session_dir.name}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #111; color: #eee; font-family: sans-serif;
          display: flex; flex-direction: column; align-items: center;
          min-height: 100vh; padding: 16px; }}
  h1 {{ font-size: 1rem; opacity: .5; margin-bottom: 12px; }}
  #viewer {{ max-width: 100%; max-height: 70vh; border: 1px solid #333; }}
  #controls {{ display: flex; align-items: center; gap: 12px; margin-top: 12px; }}
  button {{ background: #222; color: #eee; border: 1px solid #444;
            padding: 6px 16px; cursor: pointer; border-radius: 4px; font-size: .9rem; }}
  button:hover {{ background: #333; }}
  input[type=range] {{ width: 260px; }}
  #label {{ min-width: 80px; text-align: center; font-size: .9rem; opacity: .7; }}
</style>
</head>
<body>
<h1>Battle Replay — {session_dir.name}</h1>
<img id="viewer" src="">
<div id="controls">
  <button id="prev">&#8592; Prev</button>
  <input type="range" id="slider" min="0" max="0" value="0">
  <button id="next">Next &#8594;</button>
  <span id="label"></span>
</div>
<script>
  const slides = [{slides_js}];
  const img    = document.getElementById('viewer');
  const slider = document.getElementById('slider');
  const label  = document.getElementById('label');
  let cur = 0;
  slider.max = slides.length - 1;

  function show(n) {{
    cur = Math.max(0, Math.min(slides.length - 1, n));
    img.src      = slides[cur];
    slider.value = cur;
    label.textContent = `Turn ${{cur + 1}} / ${{slides.length}}`;
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

# ── TTS TCP listener ──────────────────────────────────────────────────────────
# TTS owns port 39998 as the server. We connect to it as a client and keep the
# connection open — sendExternalMessage() in Lua pushes JSON to us over that
# persistent connection.

TTS_RECONNECT_INTERVAL = 3   # seconds between connection attempts

def _dispatch_action(action: str):
    if action == "capture":
        threading.Thread(target=_delayed_capture, daemon=True).start()
    elif action == "capture_auto":
        threading.Thread(target=_delayed_capture,
                         kwargs={"skip_on_unstable": True},
                         daemon=True).start()

def _listener_thread():
    notify("TTS Replay", "Connecting to TTS…")

    buf = ""
    while _state["listening"]:
        # ── Connect ───────────────────────────────────────────────────────────
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(TTS_RECONNECT_INTERVAL)
            sock.connect(("localhost", TTS_LISTEN_PORT))
            sock.settimeout(None)   # block on recv once connected
        except (ConnectionRefusedError, socket.timeout, OSError):
            # TTS not open yet — wait and retry
            sock.close()
            for _ in range(TTS_RECONNECT_INTERVAL * 10):
                if not _state["listening"]:
                    return
                time.sleep(0.1)
            continue

        notify("TTS Replay", "Connected to TTS — waiting for capture signal")
        buf = ""

        # ── Read loop — messages are newline-delimited JSON ───────────────────
        try:
            while _state["listening"]:
                chunk = sock.recv(4096)
                if not chunk:
                    break   # TTS disconnected
                buf += chunk.decode("utf-8", errors="ignore")

                # TTS may send multiple messages in one recv; process all
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if isinstance(msg, dict):
                            action = (msg.get("customMessage") or {}).get("action")
                            if action:
                                _dispatch_action(action)
                    except json.JSONDecodeError:
                        pass

        except Exception:
            pass
        finally:
            sock.close()

        if _state["listening"]:
            notify("TTS Replay", "TTS disconnected — reconnecting…")

def _grab_region(monitor: dict):
    """Grab the configured screen region and return a flat bytes object."""
    import mss
    with mss.mss() as sct:
        raw = sct.grab(monitor)
    return bytes(raw.rgb)

def _frames_stable(a: bytes, b: bytes) -> bool:
    """Return True if two raw RGB byte strings are close enough to call stable."""
    if len(a) != len(b):
        return False
    total  = len(a) // 3          # number of pixels
    matches = sum(1 for i in range(0, len(a), 3)
                  if a[i] == b[i] and a[i+1] == b[i+1] and a[i+2] == b[i+2])
    return (matches / total) >= STABILITY_THRESHOLD

def _delayed_capture(skip_on_unstable: bool = False):
    cfg    = load_config()
    region = cfg.get("region")
    if not region or region.get("width", 0) < 10 or region.get("height", 0) < 10:
        return

    monitor = {k: region[k] for k in ("left", "top", "width", "height")}

    # Initial settle wait
    time.sleep(CAMERA_SETTLE_MS / 1000)

    # Poll until stable or max attempts reached
    prev = _grab_region(monitor)
    polls = 0
    while polls < STABILITY_MAX_POLLS:
        time.sleep(STABILITY_POLL_MS / 1000)
        curr = _grab_region(monitor)
        if _frames_stable(prev, curr):
            break
        prev = curr
        polls += 1

    if polls == STABILITY_MAX_POLLS:
        if skip_on_unstable:
            notify("TTS Replay", "⚠ Auto-capture skipped — camera still moving")
            return
        notify("TTS Replay", "⚠ Camera may not have settled — frame captured anyway")

    take_screenshot(prefetched_monitor=monitor)

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
            notify("TTS Replay", f"Compiling {_state['frame_num']} frames into replay…")
            html_path = compile_html(session_dir)
            if html_path:
                notify("TTS Replay", f"Saved: {html_path.name}  —  opening folder")
                os.startfile(str(session_dir))
            else:
                notify("TTS Replay", "Export failed — no valid frames found")
        threading.Thread(target=_compile, daemon=True).start()
    else:
        notify("TTS Replay", "Session stopped (no frames captured)")


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
        if s["start"] is None:
            return
        x0, y0 = s["start"]
        x1, y1 = e.x, e.y
        if abs(x1 - x0) < 10 or abs(y1 - y0) < 10:
            return   # Ignore accidental single-pixel clicks
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
        _update_ui()   # re-evaluate has_region so Start Session enables immediately
        notify("TTS Replay", "Region saved")
    else:
        notify("TTS Replay", "Calibration cancelled")

def do_calibrate():
    threading.Thread(target=_run_calibrate, daemon=True).start()

# ── Fix screenshot glitches ───────────────────────────────────────────────────

# ── Taskbar window ───────────────────────────────────────────────────────────

def _update_ui():
    """Refresh button states and status indicator. Safe to call from any thread."""
    win = _state.get("window")
    if not win or not win.winfo_exists():
        return
    win.after(0, _apply_ui_state)

def _apply_ui_state():
    """Must run on the main thread."""
    listening  = _state["listening"]
    has_region = "region" in load_config()
    frame_num  = _state.get("frame_num", 0)

    if listening:
        _state["status_var"].set(
            f"● RECORDING  —  {frame_num} frame{'s' if frame_num != 1 else ''} captured"
        )
        _state["indicator"].config(bg="#2dce6a")
        _state["btn_session"].config(
            text="■  Stop Session", bg="#c8391a",
            command=stop_listening
        )
    else:
        _state["indicator"].config(bg="#c8391a")
        _state["btn_session"].config(
            text="▶  Start Session", bg="#2dce6a",
            command=start_listening,
            state="normal" if has_region else "disabled"
        )
        if not has_region:
            _state["status_var"].set("Not calibrated — use Calibrate Region first")
        elif frame_num == 0:
            _state["status_var"].set("Ready — start a session to begin recording")

def _exit_app():
    """Warn before exiting if a session is active with unsaved frames."""
    from tkinter import messagebox
    if _state["listening"] and _state.get("frame_num", 0) > 0:
        answer = messagebox.askyesnocancel(
            "TTS Replay — Exit?",
            f"You have {_state['frame_num']} captured frame(s) in an active session.\n\n"
            "Do you want to compile them into a replay before exiting?\n\n"
            "  Yes   → compile replay, then exit\n"
            "  No    → exit without saving\n"
            "  Cancel → go back",
            icon="warning",
        )
        if answer is True:
            stop_listening()
            time.sleep(3)
            _state["window"].destroy()
        elif answer is False:
            _state["listening"] = False
            _state["window"].destroy()
        # Cancel → do nothing
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
    _state["status_var"] = tk.StringVar(value="Initialising…")

    # ── Indicator strip + status ──────────────────────────────────────────────
    top = tk.Frame(win, bg="#1a1e26")
    top.pack(fill="x", padx=12, pady=(12, 4))

    indicator = tk.Label(top, width=2, bg="#c8391a", relief="flat")
    indicator.pack(side="left", fill="y", padx=(0, 8))
    _state["indicator"] = indicator

    tk.Label(top, textvariable=_state["status_var"],
             bg="#1a1e26", fg="#c8d4e0",
             font=("Segoe UI", 9), anchor="w",
             wraplength=300, justify="left").pack(side="left", fill="x", expand=True)

    # ── Buttons ───────────────────────────────────────────────────────────────
    btn_frame = tk.Frame(win, bg="#1a1e26")
    btn_frame.pack(fill="x", padx=12, pady=(4, 12))

    btn_opts = dict(font=("Segoe UI", 10, "bold"), relief="flat",
                    padx=10, pady=6, cursor="hand2", fg="#ffffff", width=22)

    btn_session = tk.Button(btn_frame, text="▶  Start Session",
                            bg="#2dce6a", **btn_opts)
    btn_session.pack(fill="x", pady=(0, 4))
    _state["btn_session"] = btn_session

    tk.Button(btn_frame, text="🎯  Calibrate Region",
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
