# Build the TTS Saved Object (spawnable token, no script pasting) from snapshotbot.lua.
# Run after any Lua change, then respawn the token in TTS from Objects > Saved Objects.
# Vessel replicates Shinebot's: a built-in PiecePack_Arms wooden token (no custom assets).
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Saves/Saved Objects/Tools/Snapshotbot.json"
)
SHINEBOT_PNG = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Saves/Saved Objects/Tools/Shinebot.png"
)

with open(os.path.join(HERE, "snapshotbot.lua"), encoding="utf-8") as f:
    lua = f.read()

token = {
    "SaveName": "",
    "GameMode": "",
    "Gravity": 0.5,
    "PlayArea": 0.5,
    "Date": "",
    "Table": "",
    "Sky": "",
    "Note": "",
    "Rules": "",
    "XmlUI": "",
    "LuaScript": "",
    "LuaScriptState": "",
    "ObjectStates": [
        {
            "Name": "PiecePack_Arms",
            "Transform": {
                "posX": 0.0, "posY": 1.0, "posZ": 0.0,
                "rotX": 0.0, "rotY": 180.0, "rotZ": 0.0,
                "scaleX": 1.0, "scaleY": 1.0, "scaleZ": 1.0,
            },
            "Nickname": "Snapshotbot",
            "Description": "Drop on an LCT table — records the game to "
                           "snapshotbot-production.up.railway.app automatically. "
                           "Replay link appears in chat.",
            "GMNotes": "",
            "AltLookAngle": {"x": 0.0, "y": 0.0, "z": 0.0},
            "ColorDiffuse": {"r": 1.0, "g": 1.0, "b": 1.0},
            "LayoutGroupSortIndex": 0,
            "Value": 0,
            "Locked": False,
            "Grid": False,
            "Snap": False,
            "IgnoreFoW": False,
            "MeasureMovement": False,
            "DragSelectable": True,
            "Autoraise": True,
            "Sticky": False,
            "Tooltip": True,
            "GridProjection": False,
            "HideWhenFaceDown": False,
            "Hands": False,
            "MeshIndex": -1,
            "LuaScript": lua,
            "LuaScriptState": "",
            "XmlUI": "",
            "GUID": "5b0741",
        }
    ],
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(token, f, indent=2, ensure_ascii=False)
print("written", OUT)

# Placeholder tile thumbnail (same wooden token look); TTS regenerates it on re-save.
png = OUT[:-5] + ".png"
if not os.path.exists(png) and os.path.exists(SHINEBOT_PNG):
    shutil.copy(SHINEBOT_PNG, png)
    print("thumbnail placeholder copied")
