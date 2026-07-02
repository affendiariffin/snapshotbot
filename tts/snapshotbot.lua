--[[ Snapshotbot 2.0 — LCT game capture token.
     Distributed as a Saved Object (no manual pasting). Reads LCT state via TTS APIs
     and POSTs JSON to the Railway service. Models-only tracking: terrain/objectives
     come from the server's layout library, never captured here.
     Filled in by Tasks 4 (session/scores/cards) and 9 (models/buttons). --]]

SERVER_URL = "https://REPLACE-AFTER-DEPLOY.up.railway.app"

-- LCT object GUIDs (verified against workshop 3710681747, 2026-07-03).
-- Every lookup must fall back to nickname search if the GUID is gone after a mod update.
SCORESHEET_GUID = "06d627"   -- script_state: scores[player][round][k] k=2 pri k=3 sec, endOfBattle
START_MENU_GUID = "738804"   -- getVar: debugCurrentMapName, red/blueDispositionSelected
ROUND_COUNTER_GUID = "ee92cf" -- script_state: {value = N}
CARD_ZONES = {
    red_primary = {"d1e001", "d1e002"},
    blue_primary = {"d1e003", "d1e004"},
    deployment = {"d1e005"},
    twist = {"d1e006"},
    red_secondary = {"c1e001", "c1e002", "c1e003", "c1e004", "c1e005", "c1e006", "c1e007", "c1e008"},
    blue_secondary = {"b1e001", "b1e002", "b1e003", "b1e004", "b1e005", "b1e006", "b1e007", "b1e008"},
}

function onLoad()
    broadcastToAll("[Snapshotbot] token loaded — capture not implemented yet (Task 4)", {0.6, 0.6, 0.6})
end
