
-- ═════════════════════════════════════════════════════════════════════════════
-- Fendi's Snapshotbot  (single-object script)
-- Attach this script to ONE object in your TTS save.
-- Right-click object → Scripting → paste → Save & Play.
-- ═════════════════════════════════════════════════════════════════════════════

-- ─────────────────────────────────────────────────────────────────────────────
-- CONFIG
-- ─────────────────────────────────────────────────────────────────────────────

local TOP_DOWN_POSITION = {
    x = 0,    -- centre of battlefield (X axis)
    y = 10,   -- height above table
    z = 0,    -- centre of battlefield (Z axis)
}

local TOP_DOWN_DISTANCE = 40
local CAPTURE_INTERVAL  = 60    -- seconds between auto-captures

-- ─────────────────────────────────────────────────────────────────────────────
-- GUIDs
-- ─────────────────────────────────────────────────────────────────────────────

local SCORESHEET_GUID = "06d627"

local ZONE_GUIDS = {
    deployment = "dcf95b",
    primary    = "740abc",
    challenger = "cdecf2",
    p1_sec1    = "0ec215",
    p1_sec2    = "d865d4",
    p2_sec1    = "3c8d71",
    p2_sec2    = "88cac4",
}

-- ─────────────────────────────────────────────────────────────────────────────
-- Helpers
-- ─────────────────────────────────────────────────────────────────────────────

local C = {
    green  = {0.18, 0.8,  0.42},
    yellow = {1.0,  0.85, 0.2 },
    red    = {0.9,  0.2,  0.2 },
    grey   = {0.6,  0.6,  0.6 },
    orange = {1.0,  0.5,  0.0 },
}

local function log(msg, color)
    broadcastToAll("[SnapBot] " .. tostring(msg), color or C.grey)
end

-- Detailed error logger: prints context name + error + a hint.
-- Call as:  safeCall(context_label, function() ... end)
-- Returns true on success, false on error.
local function safeCall(label, fn)
    local ok, err = pcall(fn)
    if not ok then
        log("ERROR in " .. label .. ": " .. tostring(err), C.red)
    end
    return ok
end

-- ─────────────────────────────────────────────────────────────────────────────
-- Cross-context data store — uses self.setVar / self.getVar so data written
-- in onExternalMessage can be safely read in onUpdate and vice-versa.
-- Upvalue variables are context-isolated in TTS non-Global object scripts;
-- object vars are not.
-- ─────────────────────────────────────────────────────────────────────────────

local function storeData(scores, cards)
    -- Only overwrite a slot when we have a real value; passing nil preserves
    -- whatever was last successfully cached.
    if scores ~= nil then
        self.setVar("cachedScores", JSON.encode(scores))
    end
    if cards ~= nil then
        self.setVar("cachedCards", JSON.encode(cards))
    end
end

local function loadData()
    local s = self.getVar("cachedScores")
    local c = self.getVar("cachedCards")
    local scores = (s and s ~= "null") and JSON.decode(s) or nil
    local cards  = (c and c ~= "null") and JSON.decode(c)  or nil
    return scores, cards
end

-- ─────────────────────────────────────────────────────────────────────────────
-- State — only PRIMITIVE upvalues; never read across contexts.
-- All inter-context communication goes through self.setVar / self.getVar.
-- ─────────────────────────────────────────────────────────────────────────────

local recording      = false
local capturing      = false
local connected      = false
local recorder_color = nil
local recWaitID      = nil

local zoneObjs    = {}
local zonesCached = false

-- Primitive trigger flags — written by button/Wait callbacks, read by onUpdate.
local triggerCapture  = nil    -- player_color string or nil
local triggerRecStart = nil    -- player_color string or nil
local triggerRecStop  = false  -- bool
local pendingAutoCap  = false  -- bool

-- Capture pipeline phase stored on object so it survives context switches.
-- self.setVar("phase", 0/1/2)

local BTN_CAPTURE = 0
local BTN_REC     = 1

-- ─────────────────────────────────────────────────────────────────────────────
-- onLoad
-- ─────────────────────────────────────────────────────────────────────────────

function onLoad()
    -- Initialise object vars
    self.setVar("cachedScores", "null")
    self.setVar("cachedCards",  "null")
    self.setVar("phase",        0)
    self.setVar("pendingAction",      "")
    self.setVar("pendingPlayerColor", "")

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

    log("Loaded. Waiting for Python connection...", C.grey)
    WebRequest.post("http://127.0.0.1:39997/handshake", '{"action":"handshake"}', function(req) end)
end

-- ─────────────────────────────────────────────────────────────────────────────
-- Zone cache + data refresh — called ONLY from onExternalMessage
-- ─────────────────────────────────────────────────────────────────────────────

local function _ensureZones()
    if zonesCached then return end
    local allFound = true
    for key, guid in pairs(ZONE_GUIDS) do
        local obj = getObjectFromGUID(guid)
        if obj then zoneObjs[key] = obj else allFound = false end
    end
    zonesCached = allFound
end

local function _firstName(zone)
    if not zone then return nil end
    local objs = zone.getObjects()
    for _, obj in ipairs(objs) do
        if obj.type ~= "Deck" then
            local name = obj.getName()
            if name ~= "" then return name end
        end
    end
    return nil
end

local function _refreshCache()
    -- Called only from onExternalMessage — safe context for cross-object calls.
    -- Cards: zone.getObjects() property reads.
    -- Scores: sheet.script_state property read (NOT sheet.call — no ownership error).

    local cards  = nil
    local scores = nil

    pcall(function()
        _ensureZones()
        cards = {
            deployment = _firstName(zoneObjs.deployment),
            primary    = _firstName(zoneObjs.primary),
            challenger = _firstName(zoneObjs.challenger),
            p1_sec1    = _firstName(zoneObjs.p1_sec1),
            p1_sec2    = _firstName(zoneObjs.p1_sec2),
            p2_sec1    = _firstName(zoneObjs.p2_sec1),
            p2_sec2    = _firstName(zoneObjs.p2_sec2),
        }
    end)

    pcall(function()
        local sheet = getObjectFromGUID(SCORESHEET_GUID)
        if not sheet then return end
        local state = sheet.script_state  -- plain property read, safe
        if not state or state == "" then return end
        local data = JSON.decode(state)
        if not data or not data.scores then return end
        local s = data.scores

        -- k-indexes from scoresheet: 1=Challenger, 2=Secondary1, 3=Secondary2, 4=Primary
        local function safeN(v) return tonumber(v) or 0 end
        local function sumK(p, k, cap)
            local t = 0
            for j=1,5 do t = t + safeN(s[p][j][k]) end
            if cap and t > cap then t = cap end
            return t
        end
        local function rounds(p)
            local r = {}
            for j=1,5 do
                local pri = safeN(s[p][j][4])
                local sc1 = safeN(s[p][j][2])
                local sc2 = safeN(s[p][j][3])
                local chl = safeN(s[p][j][1])
                table.insert(r, {
                    round      = j,
                    primary    = pri,
                    sec1       = sc1,
                    sec2       = sc2,
                    challenger = chl,
                    total      = pri + sc1 + sc2 + chl,
                })
            end
            return r
        end

        local redPri  = sumK(1, 4, 50)
        local redSec  = math.min(sumK(1, 2, nil) + sumK(1, 3, nil), 40)
        local redChl  = sumK(1, 1, 12)
        local bluPri  = sumK(2, 4, 50)
        local bluSec  = math.min(sumK(2, 2, nil) + sumK(2, 3, nil), 40)
        local bluChl  = sumK(2, 1, 12)

        local function total(pri, sec, chl)
            return math.min(pri + sec + chl + 10, 100)
        end

        -- Read steam names directly from seated players — plain property reads, safe everywhere.
        local redName  = Player["Red"].steam_name  or "Player 1"
        local blueName = Player["Blue"].steam_name or "Player 2"
        local params = {}
        scores = {
            red  = {
                name       = redName,
                primary    = redPri,
                secondary  = redSec,
                challenger = redChl,
                painted    = 10,
                total      = total(redPri, redSec, redChl),
                rounds     = rounds(1),
            },
            blue = {
                name       = blueName,
                primary    = bluPri,
                secondary  = bluSec,
                challenger = bluChl,
                painted    = 10,
                total      = total(bluPri, bluSec, bluChl),
                rounds     = rounds(2),
            },
        }
    end)

    storeData(scores, cards)
end

-- ─────────────────────────────────────────────────────────────────────────────
-- onUpdate — ALL pipeline logic lives here.
-- Reads primitive trigger flags; reads/writes inter-context data via setVar/getVar.
-- ─────────────────────────────────────────────────────────────────────────────

function onUpdate()

    -- ── Rec-stop ──────────────────────────────────────────────────────────────
    if triggerRecStop then
        triggerRecStop = false
        safeCall("onUpdate/rec-stop", function()
            recording = false
            if recWaitID then Wait.stop(recWaitID) recWaitID = nil end
            self.editButton({ index = BTN_REC, label = "⏺ START REC", font_color = C.red })
        end)
        return
    end

    -- ── Rec-start ─────────────────────────────────────────────────────────────
    if triggerRecStart then
        local color     = triggerRecStart
        triggerRecStart = nil
        safeCall("onUpdate/rec-start", function()
            recording      = true
            recorder_color = color
            self.editButton({ index = BTN_REC, label = "⏹ STOP REC", font_color = {1, 0.4, 0.0} })
            self.setVar("pendingAction",      "capture_auto")
            self.setVar("pendingPlayerColor", color)
            self.setVar("phase", 1)
            scheduleNextCapture()
        end)
        return
    end

    -- ── Manual capture trigger ────────────────────────────────────────────────
    if triggerCapture and not capturing then
        local color    = triggerCapture
        triggerCapture = nil
        safeCall("onUpdate/capture-trigger", function()
            self.setVar("pendingAction",      "capture")
            self.setVar("pendingPlayerColor", color)
            self.setVar("phase", 1)
        end)
    end

    -- ── Auto-capture timer fired ──────────────────────────────────────────────
    if pendingAutoCap then
        pendingAutoCap = false
        safeCall("onUpdate/auto-cap", function()
            if recording and not capturing then
                self.setVar("pendingAction",      "capture_auto")
                self.setVar("pendingPlayerColor", recorder_color)
                self.setVar("phase", 1)
                scheduleNextCapture()
            end
        end)
    end

    -- ── Pipeline phase 1: position camera + request cache refresh ───────────
    -- Phase advances to 2 only after onExternalMessage receives the
    -- refresh_done echo from Python, guaranteeing scores/cards are fresh.
    local phase = self.getVar("phase")
    if phase == 1 and not capturing then
        safeCall("onUpdate/phase1", function()
            local action       = self.getVar("pendingAction")
            local player_color = self.getVar("pendingPlayerColor")

            local p = Player[player_color]
            if not p or not p.seated then
                log("Player " .. tostring(player_color) .. " not seated — skipped", C.red)
                self.setVar("phase", 0)
                return
            end

            capturing = true
            log("Capturing (" .. action .. ") for " .. player_color, C.yellow)

            local yaw = (player_color == "Red") and 180 or 0
            Player[player_color].lookAt({
                position = TOP_DOWN_POSITION,
                pitch    = 90,
                yaw      = yaw,
                distance = TOP_DOWN_DISTANCE,
            })
            -- Stay at phase 1 until Python echoes refresh_done back via
            -- onExternalMessage, which will refresh the cache then set phase 2.
            WebRequest.post("http://127.0.0.1:39997/refresh", '{"action":"refresh"}',
                function(req) end)
        end)
        return
    end

    -- ── Pipeline phase 2: send signal ────────────────────────────────────────
    if phase == 2 then
        safeCall("onUpdate/phase2", function()
            local action       = self.getVar("pendingAction")
            local player_color = self.getVar("pendingPlayerColor")
            local scores, cards = loadData()
            self.setVar("phase", 0)

            local payload = JSON.encode({ action = action, scores = scores, cards = cards })
            WebRequest.post("http://127.0.0.1:39997/capture", payload,
                function(req)
                    if req.is_error then
                        log("Signal error: " .. tostring(req.error), C.red)
                    else
                        log("Captured", C.green)
                    end
                    -- Restore camera as soon as Python responds — no fixed delay needed.
                    local p = Player[player_color]
                    if p then p.setCameraMode("ThirdPerson") end
                    capturing = false
                end
            )
        end)
        return
    end

end

-- ─────────────────────────────────────────────────────────────────────────────
-- Button callbacks — write ONE primitive flag only.
-- ─────────────────────────────────────────────────────────────────────────────

function doCapture(obj, player_color, alt_click)
    if capturing then return end
    if not connected then return end
    triggerCapture = player_color
end

function doToggleRec(obj, player_color, alt_click)
    if recording then
        triggerRecStop = true
    else
        if not connected then return end
        triggerRecStart = player_color
    end
end

-- ─────────────────────────────────────────────────────────────────────────────
-- Auto-capture scheduler
-- ─────────────────────────────────────────────────────────────────────────────

function scheduleNextCapture()
    recWaitID = Wait.time(function()
        recWaitID = nil
        if not recording then return end
        pendingAutoCap = true
    end, CAPTURE_INTERVAL)
end

-- ─────────────────────────────────────────────────────────────────────────────
-- onExternalMessage — only safe context for cross-object calls
-- ─────────────────────────────────────────────────────────────────────────────

function onExternalMessage(data)
    if not data then return end

    if data.action == "handshake" then
        connected = true
        _refreshCache()
        log("Connected to Python!", C.green)
    end

    if data.action == "poll" then
        _refreshCache()
    end

    if data.action == "refresh_done" then
        -- Python has received our refresh request; now refresh the cache
        -- in this safe context before phase 2 reads it.
        _refreshCache()
        if self.getVar("phase") == 1 then
            self.setVar("phase", 2)
        end
    end

    if data.action == "reannounce" then
        connected = false
        log("Re-announce requested — reconnecting...", C.yellow)
    end
end

