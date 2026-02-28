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

-- ─────────────────────────────────────────────────────────────────────────────

-- Button indices (TTS assigns them in creation order, 0-based)
local BTN_CAPTURE = 0
local BTN_REC     = 1

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
        sendExternalMessage({ action = action_name })
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
