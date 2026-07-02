# Build the TTS Saved Object (spawnable token, no script pasting) from snapshotbot.lua.
# Run after any Lua change, then respawn the token in TTS from Objects > Saved Objects.
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Saves/Saved Objects/Snapshotbot.json"
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
            "Name": "BlockSquare",
            "Transform": {
                "posX": 0.0, "posY": 1.0, "posZ": 0.0,
                "rotX": 0.0, "rotY": 0.0, "rotZ": 0.0,
                "scaleX": 2.2, "scaleY": 0.35, "scaleZ": 1.1,
            },
            "Nickname": "Snapshotbot",
            "Description": "Drop on an LCT table — records the game to "
                           "snapshotbot-production.up.railway.app automatically.",
            "GMNotes": "",
            "ColorDiffuse": {"r": 0.07, "g": 0.08, "b": 0.12},
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
            "LuaScript": lua,
            "LuaScriptState": "",
            "XmlUI": "",
            "GUID": "5b0741",
        }
    ],
}

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(token, f, indent=2, ensure_ascii=False)
print("written", OUT)
