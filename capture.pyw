"""
TTS Battlefield Replay — System Tray App
=========================================
Double-click capture.py (or run it) to start.
A tray icon appears in the Windows system tray (bottom-right).

Tray menu:
  • Calibrate Region   — drag to select battlefield area
  • Start Session      — begin listening for TTS capture signals
  • Stop Session       — stop listening
  • Open Replay        — open playback.html in browser
  • Clean Frames       — reprocess existing frames to strip drawing lines
  • Exit

TTS Integration:
  A Lua script in TTS sends a signal on port 39998 when you press
  your capture button. This app receives it, waits for the camera
  to settle, then takes the screenshot automatically.

Requirements:
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

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE      = Path(__file__).parent / "config.json"
STORE_DIR        = Path(r"C:\Users\User\Documents\coding_projects\TTS snapshot")
SERVER_PORT      = 8080
TTS_LISTEN_PORT  = 39998   # TTS External Editor API port
CAMERA_SETTLE_MS = 500     # ms to wait after TTS snaps camera before grabbing

# ── TTS Drawing-line colours (HSV ranges, OpenCV scale: H 0-179, S/V 0-255) ──
DRAWING_COLORS = [
    # Red  H≈0, S≈228, V≈218  (measured from TTS red ruler/timer)
    ("red_lo",   0,   8, 200, 150),
    ("red_hi", 168, 179, 200, 150),
    # Teal / cyan  (TTS selection circles, H≈90)
    ("teal",    80, 100, 150,  80),
    # Bright green
    ("green",   50,  80, 200,  80),
    # Blue  H≈106, S≈226, V≈255  (measured from TTS blue ruler/timer)
    ("blue",   100, 115, 180, 150),
    # Purple / magenta
    ("purple", 130, 168, 150,  80),
    # Yellow — high S threshold to avoid desert sand tones
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
    """Grab one frame, post-process it, update manifest."""
    try:
        import mss
        from mss.tools import to_png
    except ImportError:
        notify("TTS Replay", "mss not installed — run: pip install mss")
        return

    cfg = load_config()
    if "region" not in cfg:
        notify("TTS Replay", "No region set — calibrate first")
        return

    monitor = {k: cfg["region"][k] for k in ("left", "top", "width", "height")}

    # Initialise session folder and manifest on first capture of the session
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
        manifest_path = session_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(_state["manifest"], f, indent=2)

    session_dir   = _state["session_dir"]
    manifest_path = session_dir / "manifest.json"

    ts       = datetime.now()
    turn     = _state["frame_num"] + 1
    # Filename includes timestamp so frames are self-documenting on disk
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

    # Write latest.json so playback.html always knows which session to load
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
    """
    Listens on TTS_LISTEN_PORT for the TTS External Editor API message.
    TTS Lua sends: sendExternalMessage({action="capture"})
    which arrives as JSON: {"messageID":4, "customMessage":{"action":"capture"}}
    """
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
    """Generate a simple tray icon — red circle on dark background."""
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60],  outline="#c8391a", width=5)
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
        pystray.MenuItem(
            "Calibrate Region",
            lambda icon, item: do_calibrate()
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Stop Session" if listening else "Start Session",
            lambda icon, item: stop_listening() if _state["listening"] else start_listening(),
            enabled=has_region
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Open Replay in Browser",
            lambda icon, item: _open_replay()
        ),
        pystray.MenuItem(
            "Clean Existing Frames",
            lambda icon, item: do_clean()
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda icon, item: _exit_app()),
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

    icon = pystray.Icon(
        name  = "TTS Replay",
        icon  = _make_icon(),
        title = "TTS Battlefield Replay",
        menu  = _build_menu(),
    )
    _state["tray"] = icon
    ensure_server()
    icon.run()
