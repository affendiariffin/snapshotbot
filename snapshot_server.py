"""
SnapshotBot Server — receives JSON snapshots from TTS via HTTP POST and
writes them to disk. Runs as a Windows system tray application.

Double-click SnapshotBot.exe (compiled with build.bat) before launching TTS.
Right-click the tray icon to open the snapshot folder or quit.
"""

import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import pystray
from PIL import Image, ImageDraw

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------
PORT      = 39999
SAVE_DIR  = os.path.join(os.path.expanduser("~"), "Documents", "TTS Snapshots")

# -----------------------------------------------------------------------
# Globals
# -----------------------------------------------------------------------
_icon           = None
_snapshot_count = 0
_lock           = threading.Lock()


# -----------------------------------------------------------------------
# HTTP handler
# -----------------------------------------------------------------------
class SnapshotHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        global _snapshot_count, _icon

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        filename = self.headers.get("X-Filename", "snapshot.json")

        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            filepath = os.path.join(SAVE_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(body.decode("utf-8"))

            with _lock:
                _snapshot_count += 1
                count = _snapshot_count

            if _icon:
                _icon.title = f"SnapshotBot \u2014 {count} snapshot(s) saved"

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def do_GET(self):
        # Simple health-check so TTS can verify the server is up
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress console noise


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# -----------------------------------------------------------------------
# Tray icon
# -----------------------------------------------------------------------
def _make_icon_image():
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Green circle background
    draw.ellipse([2, 2, 62, 62], fill=(46, 125, 50, 255))

    # Camera body (white rectangle)
    draw.rectangle([12, 20, 52, 44], fill=(255, 255, 255, 230), outline=None)

    # Camera bump (top centre)
    draw.rectangle([24, 14, 40, 22], fill=(255, 255, 255, 230))

    # Lens (dark green circle inside white body)
    draw.ellipse([20, 24, 44, 40], fill=(30, 80, 32, 255))
    draw.ellipse([25, 27, 39, 37], fill=(255, 255, 255, 200))

    return img


def _open_folder(icon, item):
    os.makedirs(SAVE_DIR, exist_ok=True)
    os.startfile(SAVE_DIR)


def _quit(icon, item):
    icon.stop()
    os._exit(0)


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------
def main():
    global _icon

    server = _ThreadedHTTPServer(("localhost", PORT), SnapshotHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    _icon = pystray.Icon(
        "SnapshotBot",
        _make_icon_image(),
        "SnapshotBot \u2014 Ready",
        menu=pystray.Menu(
            pystray.MenuItem("Open Snapshot Folder", _open_folder),
            pystray.MenuItem("Quit", _quit),
        ),
    )
    _icon.run()


if __name__ == "__main__":
    main()
