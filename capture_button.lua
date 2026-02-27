-- TTS Battlefield Replay — Capture Button
-- Attach this script to any object in your TTS save.
-- Right-click object → Scripting → paste → Save & Play.
--
-- BUTTONS:
--   📷 CAPTURE   — single manual capture (snap top-down, grab, restore camera)
--   ⏺ START REC  — auto-captures every CAPTURE_INTERVAL seconds until TTS closes
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
local recorder_color  = nil   -- player color that clicked START REC
local loop_timer      = nil

-- ─────────────────────────────────────────────────────────────────────────────

function onLoad()
    self.setScale({0.3, 0.3, 0.3})

    -- Manual single capture
    self.createButton({
        label          = "📷 CAPTURE",
        click_function = "doCapture",
        function_owner = self,
        position       = {0, 0.5, -1.2},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {1, 0.85, 0.2},
        tooltip        = "Capture this moment",
    })

    -- Start auto-record loop
    self.createButton({
        label          = "⏺ START REC",
        click_function = "doStartRec",
        function_owner = self,
        position       = {0, 0.5, 1.2},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {0.9, 0.2, 0.2},
        tooltip        = "Auto-capture every " .. CAPTURE_INTERVAL .. "s (runs until you exit TTS)",
    })
end

-- ─────────────────────────────────────────────────────────────────────────────
-- Shared capture sequence: save camera → snap top-down → signal Python → restore
-- ─────────────────────────────────────────────────────────────────────────────

function runCaptureSequence(player_color)
    local player = Player[player_color]
    if player == nil then return end

    -- Save current camera state so we can restore it afterwards
    local saved_position = player.getHandTransform() -- positional reference
    local saved_mode     = "ThirdPerson"             -- we always restore to ThirdPerson

    -- Snap to top-down
    player.setCameraMode("TopDown")
    player.lookAt({
        position = TOP_DOWN_POSITION,
        pitch    = 90,
        yaw      = 0,
        distance = TOP_DOWN_DISTANCE,
    })

    -- Wait for camera to settle, then signal Python
    Wait.time(function()
        sendExternalMessage({ action = "capture" })

        -- Restore camera after Python has had time to grab the screenshot
        Wait.time(function()
            player.setCameraMode(saved_mode)
        end, 0.8)

    end, 1.0)
end

-- ─────────────────────────────────────────────────────────────────────────────
-- CAPTURE button — single manual grab
-- ─────────────────────────────────────────────────────────────────────────────

function doCapture(obj, player_color, alt_click)
    runCaptureSequence(player_color)
end

-- ─────────────────────────────────────────────────────────────────────────────
-- START REC button — fires immediately, then loops every CAPTURE_INTERVAL secs
-- ─────────────────────────────────────────────────────────────────────────────

function doStartRec(obj, player_color, alt_click)
    if recording then
        -- Already running — notify the clicker and ignore
            return
    end

    recording      = true
    recorder_color = player_color

    -- Fire the first capture immediately
    runCaptureSequence(recorder_color)

    -- Then schedule the repeating loop
    scheduleNextCapture()
end

function scheduleNextCapture()
    loop_timer = Wait.time(function()
        if not recording then return end
        runCaptureSequence(recorder_color)
        scheduleNextCapture()   -- re-schedule for the next interval
    end, CAPTURE_INTERVAL)
end
