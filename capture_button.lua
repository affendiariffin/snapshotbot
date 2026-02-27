-- TTS Battlefield Replay — Capture Button
-- Attach this script to any object in your TTS save.
-- Right-click object → Scripting → paste → Save & Play.
--
-- Set TOP_DOWN_POSITION to the centre of your battlefield (X, Y, Z).
-- Set TOP_DOWN_DISTANCE to control zoom — increase for larger boards.

local TOP_DOWN_POSITION = {
    x = 0,    -- centre of battlefield (X axis)
    y = 10,   -- height above table
    z = 0,    -- centre of battlefield (Z axis)
}

local TOP_DOWN_DISTANCE = 40

-- ─────────────────────────────────────────────────────────────────────────────

function onLoad()
    self.setScale({0.3, 0.3, 0.3})

    self.createButton({
        label          = "📷 CAPTURE",
        click_function = "doCapture",
        function_owner = self,
        position       = {0, 0.5, 0},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 600,
        font_size      = 120,
        color          = {0.1, 0.1, 0.1},
        font_color     = {1, 0.85, 0.2},
        tooltip        = "Capture",
    })
end

function doCapture(obj, player_color, alt_click)
    -- Use whoever pressed the button as the camera to snap
    local player = Player[player_color]

    player.setCameraMode("TopDown")
    player.lookAt({
        position = TOP_DOWN_POSITION,
        pitch    = 90,
        yaw      = 0,
        distance = TOP_DOWN_DISTANCE,
    })

    Wait.time(function()
        sendExternalMessage({ action = "capture" })
        Wait.time(function()
            player.setCameraMode("ThirdPerson")
        end, 0.8)
    end, 2.5)
end
