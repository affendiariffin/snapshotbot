"""
TTS Battlefield Replay — System Tray App
=========================================
Double-click to start. A tray icon appears in the Windows system tray.

Tray menu:
  • Calibrate Region        — drag to select battlefield area
  • Start Session           — begin listening for TTS capture signals
  • Stop Session            — stop listening (auto-compiles replay.mp4)
  • Fix Screenshot Glitches — reprocess existing frames to strip drawing lines
  • Setup Guide             — step-by-step setup wizard
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

TTS_LISTEN_PORT  = 39998
CAMERA_SETTLE_MS = 500

# ── Embedded assets ───────────────────────────────────────────────────────────


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
  Right-click the tray icon → Stop Session.
  A replay.mp4 is automatically compiled and the session folder opens.

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
    "is_first_run": False,   # set True by ensure_assets when config.json is absent
}

# ── First-run setup ───────────────────────────────────────────────────────────

def ensure_assets():
    """Write capture_button.lua on first run if missing. Flag first-run state."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)

    if not CONFIG_FILE.exists():
        _state["is_first_run"] = True

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
    _update_tray_menu()   # keep frame counter in tray status line current

# ── Video export ─────────────────────────────────────────────────────────────

def compile_video(session_dir: Path) -> Path | None:
    """Stitch all turn_*.png frames in order into replay.mp4."""
    try:
        import cv2
    except ImportError:
        notify("TTS Replay", "cv2 not available — cannot compile video")
        return None

    frames = sorted(session_dir.glob("turn_*.png"))
    if not frames:
        return None

    sample = cv2.imread(str(frames[0]))
    if sample is None:
        return None
    h, w = sample.shape[:2]

    video_path = session_dir / "replay.mp4"
    fps    = 2   # 2 fps → each turn shown for 0.5 s; adjust to taste
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(video_path), fourcc, fps, (w, h))

    for fp in frames:
        frame = cv2.imread(str(fp))
        if frame is not None:
            writer.write(frame)

    writer.release()
    return video_path if video_path.exists() else None

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
            while len(data) < 65536:     # 64 KB cap — TTS messages are tiny
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
    _update_tray_menu()   # also refreshes icon to green

def stop_listening():
    if not _state["listening"]:
        return
    _state["listening"] = False
    _update_tray_menu()

    session_dir = _state.get("session_dir")
    if session_dir and _state.get("frame_num", 0) > 0:
        def _compile():
            notify("TTS Replay", f"Compiling {_state['frame_num']} frames into video…")
            video_path = compile_video(session_dir)
            if video_path:
                notify("TTS Replay", f"Saved: {video_path.name}  —  opening folder")
                os.startfile(str(session_dir))   # Open session folder in Explorer
            else:
                notify("TTS Replay", "Video compile failed — no valid frames found")
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
        notify("TTS Replay", "Region saved — check the preview window!")
        _show_region_preview(result["region"])
    else:
        notify("TTS Replay", "Calibration cancelled")

def do_calibrate():
    threading.Thread(target=_run_calibrate, daemon=True).start()

# ── Setup Wizard ─────────────────────────────────────────────────────────────

WIZARD_STEPS = [
    {
        "title": "Welcome to TTS Battlefield Replay!",
        "emoji": "🎮",
        "body": (
            "This app records your Tabletop Simulator battles turn-by-turn "
            "and compiles them into a video so you can relive the carnage.\n\n"
            "Setup takes about 2 minutes and you only ever have to do it once.\n\n"
            "Click Next to get started!"
        ),
        "action_label": None,
    },
    {
        "title": "Step 1 — Enable TTS External API",
        "emoji": "🔌",
        "body": (
            "In Tabletop Simulator, open the menu and go to:\n\n"
            "    Configuration  →  External Editor API\n\n"
            "Make sure it is turned ON.\n"
            "The port should be 39998 (the default — don't change it).\n\n"
            "This lets the app talk to TTS."
        ),
        "action_label": None,
    },
    {
        "title": "Step 2 — Add the Capture Button to TTS",
        "emoji": "📋",
        "body": (
            "In TTS, right-click any small object (a coin or token works great).\n\n"
            "Choose:  Scripting  →  open the Lua editor\n\n"
            "Click the button below to copy the Lua script to your clipboard, "
            "then paste it into the TTS Lua editor and click  Save & Play.\n\n"
            "A  📷 CAPTURE  button will appear on the object."
        ),
        "action_label": "📋  Copy Lua Script to Clipboard",
    },
    {
        "title": "Step 3 — Calibrate the Capture Region",
        "emoji": "🎯",
        "body": (
            "Now tell the app which part of your screen is the battlefield.\n\n"
            "Click the button below, then drag a rectangle over your "
            "battlefield area.\n\n"
            "Tip: include a little border around the board so nothing gets "
            "cut off at the edges."
        ),
        "action_label": "🎯  Calibrate Region Now",
    },
    {
        "title": "Step 4 — You're Ready to Play!",
        "emoji": "✅",
        "body": (
            "Setup is complete. Here's how to use it each game:\n\n"
            "  1.  Right-click the tray icon  →  Start Session\n"
            "  2.  Play your game normally\n"
            "  3.  Press  📷 CAPTURE  in TTS at the end of each turn\n"
            "  4.  When the game ends, right-click the tray  →  Stop Session\n"
            "  5.  Your  replay.mp4  is compiled automatically!\n\n"
            "The tray icon turns green while recording is active."
        ),
        "action_label": None,
    },
]

def do_show_instructions(start_step: int = 0):
    import tkinter as tk

    def _run():
        step_idx = [start_step]

        root = tk.Tk()
        root.title("TTS Replay — Setup Guide")
        root.geometry("560x420")
        root.resizable(False, False)
        root.configure(bg="#1a1e26")
        root.lift()
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))

        # ── Header bar ──────────────────────────────────────────────────────
        header = tk.Frame(root, bg="#c8391a", height=6)
        header.pack(fill="x")

        # ── Step indicator dots ──────────────────────────────────────────────
        dot_frame = tk.Frame(root, bg="#1a1e26", pady=12)
        dot_frame.pack(fill="x")
        dot_labels = []
        for i in range(len(WIZARD_STEPS)):
            d = tk.Label(dot_frame, text="●", bg="#1a1e26",
                         font=("Segoe UI", 10))
            d.pack(side="left", padx=4, expand=True)
            dot_labels.append(d)

        # ── Emoji + title ───────────────────────────────────────────────────
        emoji_lbl = tk.Label(root, text="", bg="#1a1e26",
                             font=("Segoe UI Emoji", 36))
        emoji_lbl.pack(pady=(0, 4))

        title_lbl = tk.Label(root, text="", bg="#1a1e26", fg="#ffffff",
                             font=("Segoe UI", 14, "bold"),
                             wraplength=500, justify="center")
        title_lbl.pack(padx=24)

        # ── Body text ────────────────────────────────────────────────────────
        body_lbl = tk.Label(root, text="", bg="#1a1e26", fg="#b0bec5",
                            font=("Segoe UI", 10),
                            wraplength=500, justify="left")
        body_lbl.pack(padx=28, pady=12, fill="both", expand=True)

        # ── Action button (step-specific) ────────────────────────────────────
        action_btn = tk.Button(
            root, text="", bg="#2a3040", fg="#ffffff",
            font=("Segoe UI", 10, "bold"), relief="flat",
            padx=12, pady=6, cursor="hand2",
            activebackground="#374055", activeforeground="#ffffff",
        )
        action_btn.pack(padx=28, pady=(0, 8), fill="x")

        # ── Navigation bar ───────────────────────────────────────────────────
        nav = tk.Frame(root, bg="#111418", pady=10)
        nav.pack(fill="x", side="bottom")

        back_btn = tk.Button(nav, text="← Back", bg="#111418", fg="#5a6878",
                             font=("Segoe UI", 10), relief="flat",
                             padx=16, cursor="hand2",
                             activebackground="#1a1e26", activeforeground="#c8d4e0")
        back_btn.pack(side="left", padx=16)

        next_btn = tk.Button(nav, text="Next →", bg="#c8391a", fg="#ffffff",
                             font=("Segoe UI", 10, "bold"), relief="flat",
                             padx=20, pady=6, cursor="hand2",
                             activebackground="#e04020", activeforeground="#ffffff")
        next_btn.pack(side="right", padx=16)

        def render(idx):
            step = WIZARD_STEPS[idx]
            emoji_lbl.config(text=step["emoji"])
            title_lbl.config(text=step["title"])
            body_lbl.config(text=step["body"])

            # Dot colours
            for i, d in enumerate(dot_labels):
                if i < idx:
                    d.config(fg="#c8391a")
                elif i == idx:
                    d.config(fg="#ffffff")
                else:
                    d.config(fg="#2a3040")

            # Action button
            if step["action_label"]:
                action_btn.config(text=step["action_label"], state="normal")
                action_btn.pack(padx=28, pady=(0, 8), fill="x")
                if "Clipboard" in step["action_label"]:
                    action_btn.config(command=_copy_lua)
                elif "Calibrate" in step["action_label"]:
                    action_btn.config(command=lambda: (do_calibrate(), notify(
                        "TTS Replay", "Drag your rectangle over the battlefield!")))
            else:
                action_btn.pack_forget()

            # Nav buttons
            back_btn.config(state="normal" if idx > 0 else "disabled",
                            fg="#c8d4e0" if idx > 0 else "#2a3040")
            if idx == len(WIZARD_STEPS) - 1:
                next_btn.config(text="✓  Done", bg="#2dce6a",
                                command=root.destroy)
            else:
                next_btn.config(text="Next →", bg="#c8391a",
                                command=lambda: go(idx + 1))

        def go(idx):
            step_idx[0] = idx
            render(idx)

        def _copy_lua():
            try:
                root.clipboard_clear()
                root.clipboard_append(CAPTURE_BUTTON_LUA)
                root.update()
                action_btn.config(text="✓  Copied! Now paste into TTS Lua editor",
                                  bg="#2dce6a")
                root.after(3000, lambda: action_btn.config(
                    text=WIZARD_STEPS[step_idx[0]]["action_label"], bg="#2a3040"))
            except Exception:
                action_btn.config(text="⚠  Could not access clipboard", bg="#e8a020")

        back_btn.config(command=lambda: go(step_idx[0] - 1))

        render(start_step)
        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()

# ── Calibration preview ───────────────────────────────────────────────────────

def _show_region_preview(region: dict):
    """Show a small preview of the calibrated capture region."""
    import tkinter as tk
    try:
        import mss
        from PIL import Image, ImageTk
    except ImportError:
        return

    def _run():
        monitor = {k: region[k] for k in ("left", "top", "width", "height")}
        with mss.mss() as sct:
            raw = sct.grab(monitor)
            img = Image.frombytes("RGB", raw.size, raw.rgb)

        # Scale preview to max 480px wide
        max_w = 480
        scale = min(1.0, max_w / img.width)
        pw = int(img.width * scale)
        ph = int(img.height * scale)
        img = img.resize((pw, ph), Image.LANCZOS)

        root = tk.Tk()
        root.title("Calibration Preview — does this look right?")
        root.configure(bg="#1a1e26")
        root.resizable(False, False)
        root.lift()
        root.attributes("-topmost", True)
        root.after(200, lambda: root.attributes("-topmost", False))

        tk.Label(root, text="Is this your battlefield?",
                 bg="#1a1e26", fg="#ffffff",
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 4))
        tk.Label(root, text="If it looks wrong, click Redo to drag again.",
                 bg="#1a1e26", fg="#b0bec5",
                 font=("Segoe UI", 9)).pack(pady=(0, 8))

        photo = ImageTk.PhotoImage(img)
        img_lbl = tk.Label(root, image=photo, bg="#000000",
                           relief="flat", bd=2)
        img_lbl.image = photo
        img_lbl.pack(padx=16, pady=4)

        btn_frame = tk.Frame(root, bg="#1a1e26")
        btn_frame.pack(pady=12, fill="x", padx=16)

        tk.Button(btn_frame, text="✓  Looks Good!", bg="#2dce6a", fg="#000000",
                  font=("Segoe UI", 10, "bold"), relief="flat",
                  padx=16, pady=6, cursor="hand2",
                  command=root.destroy).pack(side="left", expand=True, fill="x", padx=(0, 6))

        def redo():
            root.destroy()
            do_calibrate()

        tk.Button(btn_frame, text="↺  Redo", bg="#2a3040", fg="#ffffff",
                  font=("Segoe UI", 10), relief="flat",
                  padx=16, pady=6, cursor="hand2",
                  command=redo).pack(side="left", expand=True, fill="x", padx=(6, 0))

        root.mainloop()

    threading.Thread(target=_run, daemon=True).start()

# ── Fix screenshot glitches ───────────────────────────────────────────────────

def do_clean():
    def _run():
        search_dir = _state.get("session_dir") or STORE_DIR
        frames     = sorted(search_dir.glob("turn_*.png")) or sorted(search_dir.glob("frame_*.png"))
        if not frames:
            notify("TTS Replay", "No screenshots found to fix in the current session")
            return
        notify("TTS Replay", f"Fixing {len(frames)} screenshot(s)… please wait")
        changed = sum(1 for fp in frames if strip_drawing_lines(fp))
        notify("TTS Replay", f"Done! {changed} of {len(frames)} screenshot(s) were cleaned up")
    threading.Thread(target=_run, daemon=True).start()

# ── Tray icon ─────────────────────────────────────────────────────────────────

def _make_icon(recording: bool = False):
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if recording:
        # Solid green circle = actively recording
        draw.ellipse([4, 4, 60, 60],   fill="#2dce6a", outline="#1aaa50", width=3)
        draw.ellipse([22, 22, 42, 42], fill="#ffffff")
    else:
        # Red ring = idle
        draw.ellipse([4, 4, 60, 60],   outline="#c8391a", width=5)
        draw.ellipse([20, 20, 44, 44], fill="#c8391a")
    return img

def _refresh_icon():
    """Swap the tray icon to reflect current recording state."""
    if _state["tray"]:
        _state["tray"].icon = _make_icon(recording=_state["listening"])

def _build_menu():
    import pystray
    listening  = _state["listening"]
    has_region = "region" in load_config()
    frame_num  = _state.get("frame_num", 0)

    if listening:
        status_label = f"● RECORDING  —  {frame_num} frame{'s' if frame_num != 1 else ''} captured"
    else:
        if not has_region:
            status_label = "○ Not set up  —  open Setup Guide first"
        else:
            status_label = "○ Ready  —  Start Session to begin recording"

    start_stop_label   = "■  Stop Session  (compiles video)" if listening else "▶  Start Session"
    start_stop_tooltip = (
        "Stop recording and compile replay.mp4" if listening
        else ("Start recording this game session" if has_region
              else "Calibrate your region first before starting")
    )

    return pystray.Menu(
        pystray.MenuItem(status_label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            start_stop_label,
            lambda icon, item: stop_listening() if _state["listening"] else start_listening(),
            enabled=has_region,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🎯  Calibrate Region",
                         lambda icon, item: do_calibrate(),
                         ),
        pystray.MenuItem("🔧  Fix Screenshot Glitches",
                         lambda icon, item: do_clean(),
                         ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📖  Setup Guide",
                         lambda icon, item: do_show_instructions()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda icon, item: _exit_app()),
    )

def _update_tray_menu():
    if _state["tray"]:
        _state["tray"].menu = _build_menu()
    _refresh_icon()

def _exit_app():
    """Warn before exiting if a session is active with unsaved frames."""
    if _state["listening"] and _state.get("frame_num", 0) > 0:
        import tkinter as tk
        from tkinter import messagebox

        def _ask():
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            answer = messagebox.askyesnocancel(
                "TTS Replay — Exit?",
                f"You have {_state['frame_num']} captured frame(s) in an active session.\n\n"
                "Do you want to compile them into a video before exiting?\n\n"
                "  Yes   → compile video, then exit\n"
                "  No    → exit without saving\n"
                "  Cancel → go back",
                icon="warning",
            )
            root.destroy()

            if answer is True:       # Yes — compile then exit
                stop_listening()
                time.sleep(3)        # Give compile thread a moment to start
                _state["tray"].stop()
            elif answer is False:    # No — exit immediately
                _state["listening"] = False
                _state["tray"].stop()
            # Cancel → do nothing

        threading.Thread(target=_ask, daemon=True).start()
    else:
        _state["listening"] = False
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
        icon  = _make_icon(recording=False),
        title = "TTS Battlefield Replay",
        menu  = _build_menu(),
    )
    _state["tray"] = icon

    # On first run, automatically open the setup wizard after the tray is ready
    if _state["is_first_run"]:
        def _first_run_welcome():
            time.sleep(1.5)   # Let the tray icon settle first
            do_show_instructions(start_step=0)
        threading.Thread(target=_first_run_welcome, daemon=True).start()

    icon.run()
