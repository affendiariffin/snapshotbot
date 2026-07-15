# Compile the token Lua under TTS's OWN MoonSharp via the External Editor API
# (the Atom/VSCode plugin protocol). We send an Execute-Lua message to Global
# that wraps the script in load() - TTS compiles it and returns the error, if
# any; nothing is executed. This is the only interpreter whose opinion matters:
# standard Lua tools accept code MoonSharp rejects (goto past a local killed
# the 2026-07-15 token at load - see docs/Architecture.md invariant).
import json
import socket
import time

TTS = ("127.0.0.1", 39999)  # TTS listens here for editor messages
REPLY_PORT = 39998          # ...and answers to a listener on this port
RETURN_ID = 991
TIMEOUT = 12


def _brackets(lua):
    n = 1
    while "]" + "=" * n + "]" in lua:
        n += 1
    return "[" + "=" * n + "[", "]" + "=" * n + "]"


# -> ("ok" | "error" | "unavailable", detail)
def check(lua):
    op, cl = _brackets(lua)
    # Lua drops a newline that directly follows the opening long bracket, so
    # compile-error line numbers match snapshotbot.lua exactly.
    probe = (
        "if load == nil then return 'SB_NOLOAD' end\n"
        "local f, e = load(" + op + "\n" + lua + cl + ", 'snapshotbot')\n"
        "if f then return 'SB_OK' end\n"
        "return 'SB_ERR ' .. tostring(e)"
    )
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
            if msg.get("messageID") == 3:
                return ("unavailable", "probe itself errored in TTS: " + str(msg)[:300])
            if msg.get("messageID") == 5 and msg.get("returnID") in (RETURN_ID, str(RETURN_ID)):
                val = str(msg.get("returnValue", ""))
                if val == "SB_OK":
                    return ("ok", "")
                if val == "SB_NOLOAD":
                    return ("unavailable",
                            "TTS sandbox exposes no load() - can't compile-check; verify by respawn")
                if val.startswith("SB_ERR "):
                    return ("error", val[7:])
                return ("unavailable", "unexpected return: " + val[:200])
        return ("unavailable",
                "no reply from TTS in %ss - is a table loaded (not the main menu)?" % TIMEOUT)
    finally:
        listener.close()
