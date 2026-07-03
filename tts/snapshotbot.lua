--[[ Snapshotbot 2.0 — LCT game capture token.
     Distributed as a Saved Object (no manual pasting). Reads LCT state via TTS APIs
     and POSTs JSON to the Railway service. Models-only tracking: terrain/objectives
     come from the server's layout library, never captured here.
     All cross-object reads happen inside Wait.time callbacks wrapped in pcall
     (never onUpdate — TTS crashes on cross-object access there). --]]

SERVER_URL = "https://snapshotbot-production.up.railway.app"
POLL_SECONDS = 15      -- state check cadence
FORCE_POST_SECONDS = 60 -- post at least this often while a session runs

-- LCT object GUIDs (verified against workshop 3710681747, 2026-07-03).
-- Every lookup falls back to nickname search if a GUID dies in a mod update.
SCORESHEET = {guid = "06d627", nick = "10e Score Sheet"}
START_MENU = {guid = "738804", nick = "Start Menu"}
ROUND_COUNTER = {guid = "ee92cf", nick = "Game Rounds"}
TURN_COUNTERS = {
    red = {guid = "055302", nick = "Red Turns"},
    blue = {guid = "7e4111", nick = "Blue Turns"},
}
CARD_ZONES = {
    red_primary = {"d1e001", "d1e002"},
    blue_primary = {"d1e003", "d1e004"},
    deployment = {"d1e005"},
    twist = {"d1e006"},
    red_secondary = {"c1e001", "c1e002", "c1e003", "c1e004", "c1e005", "c1e006", "c1e007", "c1e008"},
    blue_secondary = {"b1e001", "b1e002", "b1e003", "b1e004", "b1e005", "b1e006", "b1e007", "b1e008"},
}
-- Start Menu's dispositionValues order (index vars point into this).
DISPOSITION_VALUES = {"Disruption", "Priority Assets", "Purge the Foe", "Reconnaissance", "Take and Hold"}

-- Model capture: unlocked minis on the mat. LCT spawns terrain Locked, and dice/
-- cards/tokens have other internal types, so type+lock filters almost everything;
-- the exclusion list catches loose clutter by nickname substring (lowercase).
MODEL_TYPES = {"Custom_Model", "Custom_Assetbundle", "Figurine"}
EXCLUDE_SUBSTR = {"snapshotbot", "measuring", "ruler", "template", "objective"}
MAT_X, MAT_Z = 31, 23

GREY = {0.6, 0.6, 0.6}
TEAL = {0.3, 0.8, 0.77}
RED = {0.9, 0.3, 0.3}

-- Marker so sibling tokens can recognise each other (common error: two tokens spawned).
IS_SNAPSHOTBOT = true

-- Globals survive Execute-Lua re-pushes during development.
sessionSlug = sessionSlug or nil
sessionPath = sessionPath or nil
lastSig = lastSig or nil
lastPostAt = lastPostAt or 0
startPending = startPending or false
teamByGuid = teamByGuid or {}  -- sticky deployment-half fallback (GMNotes overrides)

function log(msg, color)
    broadcastToAll("[Snapshotbot] " .. tostring(msg), color or GREY)
end

-- Every error lands in the server's Railway logs (visible with `railway logs`),
-- tagged with session + token GUID. Chat only sees one red line per minute.
lastErrBroadcast = lastErrBroadcast or 0

function remoteLog(level, msg)
    local body = {slug = sessionSlug, guid = self.getGUID(), level = level, msg = tostring(msg)}
    pcall(function()
        WebRequest.custom(SERVER_URL .. "/api/log", "POST", true, JSON.encode(body),
            {["Content-Type"] = "application/json"}, function() end)
    end)
    print("[Snapshotbot " .. level .. "] " .. tostring(msg))
end

function logError(msg)
    remoteLog("error", msg)
    if os.time() - lastErrBroadcast >= 60 then
        lastErrBroadcast = os.time()
        log(tostring(msg) .. " (details in server log)", RED)
    end
end

function findObj(ref)
    local o = getObjectFromGUID(ref.guid)
    if o ~= nil then return o end
    for _, x in ipairs(getAllObjects()) do
        if x.getName() == ref.nick then return x end
    end
    return nil
end

function postJson(path, body, cb)
    WebRequest.custom(SERVER_URL .. path, "POST", true, JSON.encode(body),
        {["Content-Type"] = "application/json"},
        function(req)
            if req.is_error or req.response_code >= 400 then
                if cb then cb(nil, "HTTP " .. tostring(req.response_code)) end
                return
            end
            local ok, parsed = pcall(JSON.decode, req.text)
            if cb then cb(ok and parsed or nil, ok and nil or "bad json") end
        end)
end

---------------------------------------------------------------------------
-- LCT state readers (call only from timer/click contexts, inside pcall)
---------------------------------------------------------------------------
function readCounter(ref)
    local o = findObj(ref)
    if not o or not o.script_state or o.script_state == "" then return 0 end
    local ok, st = pcall(JSON.decode, o.script_state)
    return (ok and st and st.value) or 0
end

function readScores()
    local sheet = findObj(SCORESHEET)
    if not sheet or not sheet.script_state or sheet.script_state == "" then return nil end
    local ok, st = pcall(JSON.decode, sheet.script_state)
    if not ok or not st or not st.scores then return nil end
    local out = {}
    local sides = {{key = "red", idx = 1}, {key = "blue", idx = 2}}
    for _, side in ipairs(sides) do
        local rounds = {}
        local priSum, secSum = 0, 0
        for r = 1, 5 do
            local row = (st.scores[side.idx] or {})[r] or {}
            -- k=1 is the sheet's round-sum display; k=2 primary, k=3 secondary
            local pri = math.min(tonumber(row[2]) or 0, 15)
            local sec = math.min(tonumber(row[3]) or 0, 15)
            priSum = priSum + pri
            secSum = secSum + sec
            rounds[r] = {round = r, primary = pri, secondary = sec, total = pri + sec}
        end
        priSum = math.min(priSum, 45)
        secSum = math.min(secSum, 45)
        local eob = tonumber((st.endOfBattle or {})[side.idx]) or 0
        out[side.key] = {
            primary = priSum, secondary = secSum, endOfBattle = eob, painted = 10,
            rounds = rounds,
            total = math.min(priSum + secSum + eob + 10, 100),
        }
    end
    return out
end

function readCards()
    local cards = {}
    for key, guids in pairs(CARD_ZONES) do
        local names = {}
        for _, g in ipairs(guids) do
            local zone = getObjectFromGUID(g)
            if zone ~= nil then
                for _, obj in ipairs(zone.getObjects()) do
                    local n = obj.getName()
                    if n ~= nil and n ~= "" then table.insert(names, n) end
                end
            end
        end
        cards[key] = names
    end
    return cards
end

function readMeta()
    local meta = {}
    local sm = findObj(START_MENU)
    if sm ~= nil then
        meta.map = sm.getVar("debugCurrentMapName")
        local rIdx = sm.getVar("redDispositionSelected")
        local bIdx = sm.getVar("blueDispositionSelected")
        meta.red_disposition = DISPOSITION_VALUES[tonumber(rIdx) or 0]
        meta.blue_disposition = DISPOSITION_VALUES[tonumber(bIdx) or 0]
    end
    for _, color in ipairs({"Red", "Blue"}) do
        local p = Player[color]
        if p ~= nil and p.seated then meta[string.lower(color) .. "_player"] = p.steam_name end
    end
    return meta
end

function isModelType(internalName)
    for _, t in ipairs(MODEL_TYPES) do
        if string.sub(internalName, 1, #t) == t then return true end
    end
    return false
end

function isExcluded(obj)
    local n = string.lower(obj.getName() or "")
    for _, s in ipairs(EXCLUDE_SUBSTR) do
        if string.find(n, s, 1, true) then return true end
    end
    return false
end

-- Which long half belongs to Red: the red seat's hand zone anchors it.
function redHalfSign()
    local ok, sign = pcall(function()
        local p = Player["Red"]
        if p == nil then return nil end
        local h = p.getHandTransform()
        if h == nil or h.position.z == 0 then return nil end
        return h.position.z > 0 and 1 or -1
    end)
    return ok and sign or nil
end

-- Team = GMNotes "Red"/"Blue" (the mod's own convention, set via our Tag buttons or
-- LCT's dormant hotkeys). Fallback: table half at FIRST sighting, sticky by GUID so
-- models keep their side after crossing the halfway line mid-game.
function modelTeam(obj, z, redSign)
    local gm = obj.getGMNotes()
    if gm == "Red" then return "red" end
    if gm == "Blue" then return "blue" end
    if redSign == nil then return nil end
    local g = obj.getGUID()
    if teamByGuid[g] == nil then
        teamByGuid[g] = ((z >= 0 and 1 or -1) == redSign) and "red" or "blue"
    end
    return teamByGuid[g]
end

function round(v, digits)
    local m = 10 ^ digits
    return math.floor(v * m + 0.5) / m
end

-- Yellowscribe nicknames look like "[00ff16]8/8[-] Morvenn Vahl": BBCode colour
-- wrapper + live wound counter + real name. Returns clean name, wounds ("8/8" or nil).
function cleanName(raw)
    local n = string.gsub(string.gsub(raw or "", "%[%x%x%x%x%x%x%]", ""), "%[%-%]", "")
    local wounds = string.match(n, "^%s*(%d+/%d+)%s")
    if wounds then n = string.gsub(n, "^%s*%d+/%d+%s+", "") end
    n = string.gsub(string.gsub(n, "^%s+", ""), "%s+$", "")
    return n, wounds
end

-- Yellowscribe tags every model of a unit "uuid:xxxxxxxx" — the unit handle.
function unitId(obj)
    for _, t in ipairs(obj.getTags() or {}) do
        local u = string.match(t, "^uuid:(%x+)$")
        if u then return u end
    end
    return nil
end

-- Viewer contract per model: {n=clean name, x, z (inches), r=rotY, b=[w,h] bounds
-- oval (inches — the viewer's Base Size Guide name lookup beats this; b is the
-- Hull/unknown fallback), t=team, w=wounds "cur/max", u=yellowscribe unit id}.
function readModels()
    local models = {}
    local redSign = redHalfSign()
    for _, obj in ipairs(getAllObjects()) do
        if obj ~= self and not obj.getLock() and isModelType(obj.name) and not isExcluded(obj) then
            local p = obj.getPosition()
            if math.abs(p.x) <= MAT_X and math.abs(p.z) <= MAT_Z then
                local b = obj.getBoundsNormalized().size
                local name, wounds = cleanName(obj.getName())
                table.insert(models, {
                    n = name,
                    x = round(p.x, 2), z = round(p.z, 2),
                    r = round(obj.getRotation().y, 1),
                    b = {round(b.x, 1), round(b.z, 1)},
                    t = modelTeam(obj, p.z, redSign),
                    w = wounds,
                    u = unitId(obj),
                })
            end
        end
    end
    return models
end

---------------------------------------------------------------------------
-- Duplicate-token guard: exactly one Snapshotbot may live on a table.
-- Survivor election: a token with a running session beats one without;
-- ties break to the lexicographically smallest GUID. Losers self-destruct.
---------------------------------------------------------------------------
function dedupeCheck()
    local mine = self.getGUID()
    for _, obj in ipairs(getAllObjects()) do
        if obj ~= self and not obj.isDestroyed() and obj.getName() == "Snapshotbot" then
            local ok, isBot = pcall(function() return obj.getVar("IS_SNAPSHOTBOT") end)
            if ok and isBot then
                local theirSlug = nil
                pcall(function() theirSlug = obj.getVar("sessionSlug") end)
                local theyWin
                if (theirSlug ~= nil) ~= (sessionSlug ~= nil) then
                    theyWin = theirSlug ~= nil
                else
                    theyWin = obj.getGUID() < mine
                end
                if theyWin then
                    log("duplicate Snapshotbot removed — one token per table is plenty", RED)
                    self.destruct()
                    return true
                end
            end
        end
    end
    return false
end

---------------------------------------------------------------------------
-- Session lifecycle
---------------------------------------------------------------------------
function tryStartSession()
    if startPending or sessionSlug ~= nil then return end
    local sheet = findObj(SCORESHEET)
    if sheet == nil then return end  -- not an LCT table (yet)
    startPending = true
    local ok, meta = pcall(readMeta)
    postJson("/api/session/start", {mission_meta = ok and meta or {}}, function(resp, err)
        startPending = false
        if resp == nil or not resp.ok then
            logError("session start failed (" .. tostring(err) .. ") — retrying later")
            return
        end
        sessionSlug = resp.slug
        sessionPath = resp.path
        log("recording — replay: " .. SERVER_URL .. sessionPath, TEAL)
        doSnapshot(nil)
    end)
end

function doSnapshot(markLabel)
    if sessionSlug == nil then return end
    local ok, body = pcall(function()
        return {
            slug = sessionSlug,
            round = readCounter(ROUND_COUNTER),
            mark = markLabel,
            scores = readScores() or {},
            cards = readCards(),
            models = readModels(),
        }
    end)
    if not ok then
        logError("snapshot collect failed: " .. tostring(body))
        return
    end
    postJson("/api/snapshot", body, function(resp, err)
        if resp == nil then
            if err == "HTTP 404" then
                -- Session vanished server-side (expired/removed): recover, don't spam.
                remoteLog("warn", "session " .. tostring(sessionSlug) .. " gone — starting fresh")
                log("session expired — starting a new one", GREY)
                sessionSlug = nil
                sessionPath = nil
                lastSig = nil
            else
                logError("snapshot post failed (" .. tostring(err) .. ")")
            end
        else
            lastPostAt = os.time()
        end
    end)
end

function computeSig()
    local sheet = findObj(SCORESHEET)
    local sheetState = (sheet and sheet.script_state) or ""
    return string.format("%s|%d|%d|%d", sheetState,
        readCounter(ROUND_COUNTER), readCounter(TURN_COUNTERS.red), readCounter(TURN_COUNTERS.blue))
end

function onPollTick()
    if dedupeCheck() then return end
    if sessionSlug == nil then
        tryStartSession()
        return
    end
    local ok, sig = pcall(computeSig)
    if not ok then return end
    if sig ~= lastSig or (os.time() - lastPostAt) >= FORCE_POST_SECONDS then
        lastSig = sig
        doSnapshot(nil)
    end
end

---------------------------------------------------------------------------
-- Buttons
---------------------------------------------------------------------------
function clickMark(_, playerColor)
    if sessionSlug == nil then
        log("no session yet", RED)
        return
    end
    local label = "R" .. readCounter(ROUND_COUNTER) .. " mark by " .. tostring(playerColor)
    doSnapshot(label)
    log("moment marked (" .. label .. ")", TEAL)
end

function clickLink()
    if sessionPath == nil then
        log("no session yet", RED)
        return
    end
    log("replay: " .. SERVER_URL .. sessionPath, TEAL)
end

-- Box-select models, then click Tag Red / Tag Blue. Writes GMNotes — the mod's own
-- team convention — so the assignment survives save/load and beats the half-table
-- guess. Tagging any model of a yellowscribe unit (uuid: tag) tags the whole unit.
function tagSelected(playerColor, team)
    local p = Player[playerColor]
    if p == nil then return end
    local picked, units = {}, {}
    for _, obj in ipairs(p.getSelectedObjects() or {}) do
        if not obj.getLock() and isModelType(obj.name) then
            picked[obj.getGUID()] = obj
            local u = unitId(obj)
            if u then units[u] = true end
        end
    end
    if next(units) ~= nil then
        for _, obj in ipairs(getAllObjects()) do
            local u = isModelType(obj.name) and unitId(obj)
            if u and units[u] then picked[obj.getGUID()] = obj end
        end
    end
    local count = 0
    for _, obj in pairs(picked) do
        obj.setGMNotes(team)
        teamByGuid[obj.getGUID()] = string.lower(team)
        count = count + 1
    end
    if count == 0 then
        log("box-select your models first, then click Tag " .. team, RED)
    else
        log(count .. " model(s) tagged " .. team, team == "Red" and RED or TEAL)
        doSnapshot(nil)
    end
end

function clickTagRed(_, playerColor) tagSelected(playerColor, "Red") end
function clickTagBlue(_, playerColor) tagSelected(playerColor, "Blue") end

---------------------------------------------------------------------------
-- Lifecycle
---------------------------------------------------------------------------
function onDestroy()
    -- Manual deletion is a legitimate way to stop recording: kill the timers,
    -- leave the session to seal itself server-side (~90s of silence).
    if pollTimerId then pcall(Wait.stop, pollTimerId) end
    pcall(remoteLog, "info", "token removed — session will seal itself")
end

function onSave()
    return JSON.encode({slug = sessionSlug, path = sessionPath, teams = teamByGuid})
end

function onLoad(saved)
    if saved ~= nil and saved ~= "" then
        local ok, st = pcall(JSON.decode, saved)
        if ok and st then
            sessionSlug = st.slug
            sessionPath = st.path
            teamByGuid = st.teams or {}
        end
    end
    -- No Start/Stop/End by design: recording begins when an LCT table is detected and
    -- the session seals itself server-side after ~90s of silence (players left TTS).
    -- Sized for the PiecePack_Arms vessel (Shinebot's single button: 900x280 @ {0,0.1,0}).
    -- Plain-text labels: TTS's button font drops emoji glyphs.
    self.createButton({
        label = "Mark", click_function = "clickMark", function_owner = self,
        position = {-0.26, 0.15, 0}, width = 400, height = 260, font_size = 95,
        color = {0.11, 0.13, 0.19}, font_color = {0.94, 0.75, 0.25},
        tooltip = "Bookmark this moment on the replay timeline",
    })
    self.createButton({
        label = "Link", click_function = "clickLink", function_owner = self,
        position = {0.26, 0.15, 0}, width = 400, height = 260, font_size = 95,
        color = {0.11, 0.13, 0.19}, font_color = {0.33, 0.53, 0.88},
        tooltip = "Broadcast the replay URL in chat",
    })
    self.createButton({
        label = "Tag R", click_function = "clickTagRed", function_owner = self,
        position = {-0.26, 0.15, 0.34}, width = 400, height = 260, font_size = 95,
        color = {0.11, 0.13, 0.19}, font_color = {0.88, 0.33, 0.33},
        tooltip = "Box-select models, then click to mark them as Red's",
    })
    self.createButton({
        label = "Tag B", click_function = "clickTagBlue", function_owner = self,
        position = {0.26, 0.15, 0.34}, width = 400, height = 260, font_size = 95,
        color = {0.11, 0.13, 0.19}, font_color = {0.33, 0.53, 0.88},
        tooltip = "Box-select models, then click to mark them as Blue's",
    })
    Wait.time(function() dedupeCheck() end, 2)  -- early check, before the first poll
    pollTimerId = Wait.time(onPollTick, POLL_SECONDS, -1)
    if sessionSlug ~= nil then
        log("resumed session — replay: " .. SERVER_URL .. tostring(sessionPath), TEAL)
    end
end
