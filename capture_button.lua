-- TTS Battlefield Replay — Capture Button
-- Attach this script to any object in your TTS save.
-- Right-click object → Scripting → paste → Save & Play.
--
-- Set TOP_DOWN_POSITION to the centre of your battlefield (X, Y, Z).
-- Set TOP_DOWN_DISTANCE to control zoom — increase for larger boards.
-- Set CAPTURE_INTERVAL to change how often auto-capture fires (seconds).

local TOP_DOWN_POSITION = {
    x = 0,    -- centre of battlefield (X axis)
    y = 10,   -- height above table
    z = 0,    -- centre of battlefield (Z axis)
}

local TOP_DOWN_DISTANCE  = 40
local CAPTURE_INTERVAL   = 60    -- seconds between auto-captures
-- ─────────────────────────────────────────────────────────────────────────────

local recording       = false
local recorder_color  = nil
local capturing       = false   -- true while a capture sequence is in flight

-- Button indices (TTS assigns them in creation order, 0-based)
local BTN_CAPTURE = 0
local BTN_REC     = 1

-- ─────────────────────────────────────────────────────────────────────────────

function onLoad()
    self.createButton({
        label          = "📷 CAPTURE",
        click_function = "doCapture",
        function_owner = self,
        position       = {0, 0.1, -0.5},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {1, 0.85, 0.2},
        tooltip        = "Capture this moment",
    })

    self.createButton({
        label          = "⏺ START REC",
        click_function = "doToggleRec",
        function_owner = self,
        position       = {0, 0.1, 0.5},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {0.9, 0.2, 0.2},
        tooltip        = "Auto-capture every " .. CAPTURE_INTERVAL .. "s",
    })
end

-- ─────────────────────────────────────────────────────────────────────────────
-- Data collection — hardcoded GUIDs from Global (see global Lua script)
-- ─────────────────────────────────────────────────────────────────────────────

local SCORESHEET_GUID         = "06d627"
local DEPLOYMENT_ZONE_GUID    = "dcf95b"
local PRIMARY_ZONE_GUID       = "740abc"
local CHALLENGER_ZONE_GUID    = "cdecf2"
local SEC_P1_1_ZONE_GUID      = "0ec215"   -- Player 1 (Red) secondary slot 1
local SEC_P1_2_ZONE_GUID      = "d865d4"   -- Player 1 (Red) secondary slot 2
local SEC_P2_1_ZONE_GUID      = "3c8d71"   -- Player 2 (Blue) secondary slot 1
local SEC_P2_2_ZONE_GUID      = "88cac4"   -- Player 2 (Blue) secondary slot 2

local function zoneCardName(guid)
    local zone = getObjectFromGUID(guid)
    if not zone then return nil end
    local objs = zone.getObjects()
    if #objs == 0 then return nil end
    local name = objs[1].getName()
    if name == "" then return nil end
    return name
end

local function getScores()
    local sheet = getObjectFromGUID(SCORESHEET_GUID)
    if not sheet then return nil end
    local ok, result = pcall(function() return sheet.call("getMatchSummary") end)
    if ok then return result end
    return nil
end

local function getCards()
    return {
        deployment = zoneCardName(DEPLOYMENT_ZONE_GUID),
        primary    = zoneCardName(PRIMARY_ZONE_GUID),
        challenger = zoneCardName(CHALLENGER_ZONE_GUID),
        -- Player 1 (Red) secondaries
        p1_sec1    = zoneCardName(SEC_P1_1_ZONE_GUID),
        p1_sec2    = zoneCardName(SEC_P1_2_ZONE_GUID),
        -- Player 2 (Blue) secondaries
        p2_sec1    = zoneCardName(SEC_P2_1_ZONE_GUID),
        p2_sec2    = zoneCardName(SEC_P2_2_ZONE_GUID),
    }
end

local function runSequence(player_color, action_name)
    if capturing then return end
    local player = Player[player_color]
    if player == nil then return end

    capturing = true
    player.setCameraMode("TopDown")
    player.lookAt({
        position = TOP_DOWN_POSITION,
        pitch    = 90,
        distance = TOP_DOWN_DISTANCE,
    })

    Wait.time(function()
        sendExternalMessage({ action = action_name, scores = getScores(), cards = getCards() })
        Wait.time(function()
            player.setCameraMode("ThirdPerson")
            capturing = false
        end, 0.8)
    end, 1.0)
end

-- ─────────────────────────────────────────────────────────────────────────────

function doCapture(obj, player_color, alt_click)
    -- Blocked while auto-rec is mid-sequence to avoid camera conflicts
    if not capturing then
        runSequence(player_color, "capture")
    end
end

-- ─────────────────────────────────────────────────────────────────────────────

function doToggleRec(obj, player_color, alt_click)
    if recording then
        -- Stop recording
        recording = false
        self.editButton({
            index      = BTN_REC,
            label      = "⏺ START REC",
            font_color = {0.9, 0.2, 0.2},
        })
    else
        -- Start recording
        recording      = true
        recorder_color = player_color
        self.editButton({
            index      = BTN_REC,
            label      = "⏹ STOP REC",
            font_color = {1, 0.4, 0.0},
        })
        runSequence(recorder_color, "capture_auto")
        scheduleNextCapture()
    end
end

function scheduleNextCapture()
    Wait.time(function()
        if not recording then return end
        runSequence(recorder_color, "capture_auto")
        scheduleNextCapture()
    end, CAPTURE_INTERVAL)
end
