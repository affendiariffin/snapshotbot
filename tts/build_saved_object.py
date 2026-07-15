# Inject snapshotbot.lua into the "Snapshotbot V2" Saved Object (Fendi's own vessel,
# saved from TTS - the cosmetics are his; we only own the script, name, description).
# Run after any Lua change, then respawn the token in TTS from Objects > Saved Objects.
# Also refreshes the copy the home page serves for friends to download - commit it.
import datetime
import hashlib
import json
import os
import sys

import moonsharp_check  # sibling module; the script dir is on sys.path when run directly

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Saves/Saved Objects/Tools/Snapshotbot V2.json"
)
WEB = os.path.join(HERE, "..", "server", "static", "snapshotbot-v2.json")

with open(os.path.join(HERE, "snapshotbot.lua"), encoding="utf-8") as f:
    lua = f.read()

# Gates - a token TTS can't load must never be written (a MoonSharp compile
# error kills the WHOLE script at load: no session, nothing records, and the
# failure only shows mid-game where it can't be fixed. 2026-07-15 incident).
try:
    from luaparser import ast as lua_ast, astnodes
except ImportError:
    sys.exit("gate: luaparser missing - python -m pip install luaparser")
try:
    tree = lua_ast.parse(lua)
except Exception as e:
    sys.exit(f"gate: Lua syntax error - {e}")
if any(isinstance(n, (astnodes.Goto, astnodes.Label)) for n in lua_ast.walk(tree)):
    sys.exit("gate: goto/label in the Lua - MoonSharp rejects jumps past a local "
             "(see docs/Architecture.md invariant); rewrite as guard-and-branch")
print("gate: syntax OK, no goto/label")

# Version = build date + hash of the code: automatic, unique per Lua change.
# The token self-reports it against /api/version and chats when it's stale.
version = (datetime.date.today().strftime("%Y.%m.%d") + "-"
           + hashlib.sha1(lua.encode("utf-8")).hexdigest()[:6])
lua = lua.replace('TOKEN_VERSION = "dev"', f'TOKEN_VERSION = "{version}"', 1)

# The real gate: compile the stamped script under TTS's own MoonSharp (the
# only interpreter that matters - luac/luaparser accept code it rejects).
if "--skip-tts" in sys.argv:
    print("gate: MoonSharp compile check SKIPPED (--skip-tts) - respawn in TTS to verify")
else:
    status, detail = moonsharp_check.check(lua)
    if status == "ok":
        print("gate: MoonSharp compile OK (checked in live TTS)")
    elif status == "error":
        sys.exit("gate: TTS refused to compile the script -\n      " + detail)
    else:
        sys.exit("gate: can't compile-check (" + detail + ")\n"
                 "      open TTS with any table loaded and rerun, or pass --skip-tts to bypass")

with open(OUT, encoding="utf-8") as f:
    token = json.load(f)

obj = token["ObjectStates"][0]
# The nickname is functional, not cosmetic: the duplicate-token guard and the
# capture exclusion (EXCLUDE_SUBSTR) both match on "Snapshotbot".
obj["Nickname"] = "Snapshotbot"
obj["Description"] = ("Drop on an LCT table — records the game to "
                      "snapshotbot-production.up.railway.app automatically. "
                      "Replay link appears in chat.\n\nVersion " + version +
                      " — the token says in chat when a newer one is out.")
obj["GMNotes"] = ""            # must stay empty: "Red"/"Blue" in GMNotes means team
obj["LuaScript"] = lua
obj["LuaScriptState"] = ""

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(token, f, indent=2, ensure_ascii=False)
print("written", OUT)

with open(WEB, "w", encoding="utf-8") as f:
    json.dump(token, f, indent=2, ensure_ascii=False)
print("written", WEB)
print("version", version)
