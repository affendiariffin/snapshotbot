# Inject snapshotbot.lua into the "Snapshotbot V2" Saved Object (Fendi's own vessel,
# saved from TTS — the cosmetics are his; we only own the script, name, description).
# Run after any Lua change, then respawn the token in TTS from Objects > Saved Objects.
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Saves/Saved Objects/Tools/Snapshotbot V2.json"
)

with open(os.path.join(HERE, "snapshotbot.lua"), encoding="utf-8") as f:
    lua = f.read()

with open(OUT, encoding="utf-8") as f:
    token = json.load(f)

obj = token["ObjectStates"][0]
# The nickname is functional, not cosmetic: the duplicate-token guard and the
# capture exclusion (EXCLUDE_SUBSTR) both match on "Snapshotbot".
obj["Nickname"] = "Snapshotbot"
obj["Description"] = ("Drop on an LCT table — records the game to "
                      "snapshotbot-production.up.railway.app automatically. "
                      "Replay link appears in chat.")
obj["GMNotes"] = ""            # must stay empty: "Red"/"Blue" in GMNotes means team
obj["LuaScript"] = lua
obj["LuaScriptState"] = ""

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(token, f, indent=2, ensure_ascii=False)
print("written", OUT)
