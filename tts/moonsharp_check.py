# Compile the token Lua under TTS's OWN MoonSharp via the External Editor API
# (the Atom/VSCode plugin protocol). The probe is the script itself prefixed
# with `do return 'SB_OK' end ` on the same line: MoonSharp compiles the whole
# chunk before running anything, then the immediate return skips execution
# entirely - safe on a live table, and TTS's sandbox has no load() (checked
# live 2026-07-16) so wrapping was not an option. Compile failure arrives as
# an Error message (messageID 3); line numbers match snapshotbot.lua exactly
# because the prefix adds no newline. This is the only interpreter whose
# opinion matters: standard Lua tools accept code MoonSharp rejects (goto
# past a local killed the 2026-07-15 token at load).
import json
import socket
import time

TTS = ("127.0.0.1", 39999)  # TTS listens here for editor messages
REPLY_PORT = 39998          # ...and answers to a listener on this port
RETURN_ID = 991
TIMEOUT = 12


# -> ("ok" | "error" | "unavailable", detail)
def check(lua):
    probe = "do return 'SB_OK' end " + lua
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", REPLY_PORT))
        listener.listen(5)
        listener.settimeout(TIMEOUT)
    except OSError:
        listener.close()
        return ("unavailable",
                "port 39998 is taken - close the TTS editor extension (Atom/VSCode) and rerun")
    try:
        try:
            with socket.create_connection(TTS, timeout=3) as s:
                s.sendall(json.dumps({
                    "messageID": 3, "returnID": RETURN_ID, "guid": "-1", "script": probe,
                }).encode("utf-8"))
        except OSError:
            return ("unavailable", "TTS not reachable on :39999 - open TTS with any table loaded")
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                break
            with conn:
                conn.settimeout(3)
                buf = b""
                while True:
                    try:
                        part = conn.recv(65536)
                    except socket.timeout:
                        break
                    if not part:
                        break
                    buf += part
            try:
                msg = json.loads(buf.decode("utf-8"))
            except ValueError:
                continue  # prints/other chatter TTS pushes to the same port
            if msg.get("messageID") == 3 and str(msg.get("guid", "-1")) == "-1":
                return ("error", str(msg.get("error", msg))[:500])
            if msg.get("messageID") == 5 and msg.get("returnID") in (RETURN_ID, str(RETURN_ID)):
                val = str(msg.get("returnValue", ""))
                if val == "SB_OK":
                    return ("ok", "")
                return ("unavailable", "unexpected return: " + val[:200])
        return ("unavailable",
                "no reply from TTS in %ss - is a table loaded (not the main menu)?" % TIMEOUT)
    finally:
        listener.close()
