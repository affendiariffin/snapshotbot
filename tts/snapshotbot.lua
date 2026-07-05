--[[ Snapshotbot 2.0 — LCT game capture token.
     Distributed as a Saved Object (no manual pasting). Reads LCT state via TTS APIs
     and POSTs JSON to the Railway service. Models-only tracking: terrain/objectives
     come from the server's layout library, never captured here.
     All cross-object reads happen inside Wait.time callbacks wrapped in pcall
     (never onUpdate — TTS crashes on cross-object access there). --]]

SERVER_URL = "https://snapshotbot-production.up.railway.app"
POLL_SECONDS = 5        -- state check cadence; posts only on actual change
FORCE_POST_SECONDS = 60 -- heartbeat: post at least this often while a session runs

-- LCT object GUIDs (verified against workshop 3710681747, 2026-07-03).
-- Every lookup falls back to nickname search if a GUID dies in a mod update.
SCORESHEET = {guid = "06d627", nick = "10e Score Sheet"}
START_MENU = {guid = "738804", nick = "Start Menu"}
ROUND_COUNTER = {guid = "ee92cf", nick = "Game Rounds"}
TURN_COUNTERS = {
    red = {guid = "055302", nick = "Red Turns"},
    blue = {guid = "7e4111", nick = "Blue Turns"},
}
-- Both are nicknamed just "Command Points", so the nickname fallback can't tell
-- sides apart — GUID-only lookups (wrong side is worse than none).
CP_COUNTERS = {
    red = {guid = "e446f7", nick = nil},
    blue = {guid = "deb9f2", nick = nil},
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

-- The mod's OWN active-secondary registry: 3DText per tableau slot, refreshed by
-- Global on every draw/discard/recycle. Authoritative — discarded and recycled
-- cards drop out instantly, unlike anything inferred from card positions.
SEC_NAME_GUIDS = {
    red = {"5423ba", "5423bb", "5423bc", "5423bd", "5423be"},
    blue = {"5423a5", "5423a6", "5423a7", "5423a8", "5423a9"},
}

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
teamByGuid = teamByGuid or {}  -- sticky drop-claim ownership (GMNotes overrides)
markerSide = markerSide or {}  -- status markers: which side's rack/hand placed them
sentGeom = sentGeom or {}      -- mesh keys already posted this session
geomQueue = geomQueue or {}    -- model GUIDs awaiting a one-time geometry post

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
            cp = readCounter(CP_COUNTERS[side.key]),
        }
    end
    return out
end

function readSecNames(side)
    local names = {}
    for _, g in ipairs(SEC_NAME_GUIDS[side]) do
        local o = getObjectFromGUID(g)
        if o ~= nil then
            local ok, v = pcall(function() return o.getValue() end)
            if ok and type(v) == "string" then
                v = string.gsub(string.gsub(v, "^%s+", ""), "%s+$", "")
                if #v > 2 then table.insert(names, v) end
            end
        end
    end
    return names
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
    -- Active secondaries from the mod's registry override the (dead) legacy zones.
    cards.red_secondary = readSecNames("red")
    cards.blue_secondary = readSecNames("blue")
    -- LCT's newer SEC tableau boards don't overlap the legacy ScriptingTriggers,
    -- so also capture every loose face-up card (name + position + table half);
    -- the viewer classifies primary/secondary by name. Cards in players' HANDS
    -- are excluded — drawn-but-hidden info must not leak into a shared replay.
    local inHand = {}
    for _, color in ipairs(getSeatedPlayers()) do
        local ok, held = pcall(function() return Player[color].getHandObjects() end)
        if ok and held then
            for _, h in ipairs(held) do inHand[h.getGUID()] = true end
        end
    end
    local loose = {}
    local redSign = redHalfSign()
    for _, obj in ipairs(getAllObjects()) do
        if (obj.name == "Card" or obj.name == "CardCustom")
            and not inHand[obj.getGUID()] and #loose < 60 then
            local ok, fd = pcall(function() return obj.is_face_down end)
            if ok and fd == false then
                local n = obj.getName()
                if n ~= nil and n ~= "" then
                    local p = obj.getPosition()
                    local t = nil
                    if redSign ~= nil then
                        t = ((p.z >= 0 and 1 or -1) == redSign) and "red" or "blue"
                    end
                    table.insert(loose, {n = n, x = round(p.x, 1), z = round(p.z, 1), t = t})
                end
            end
        end
    end
    cards.loose = loose
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

-- "Who placed it" beats geography: every drop by the seated Red/Blue player claims
-- still-unassigned models for that side. Fires for all objects table-wide (global
-- event, received by object scripts too); costs a table write, no WebRequest.
-- GMNotes still wins; first claim is sticky.
function onObjectDrop(playerColor, obj)
    if playerColor ~= "Red" and playerColor ~= "Blue" then return end
    local ok = pcall(function()
        if obj == nil or obj.isDestroyed() then return end
        local g = obj.getGUID()
        if isModelType(obj.name) then
            if teamByGuid[g] == nil then teamByGuid[g] = string.lower(playerColor) end
        elseif obj.name == "Custom_Token" then
            if markerSide[g] == nil then markerSide[g] = string.lower(playerColor) end
        end
    end)
    return ok
end

-- Status markers are dispensed from per-side racks of infinite bags: leaving a
-- container on red's half means red placed it. More precise than the drop claim,
-- so it overwrites; the "(Red)"/"(Blue)" name suffix wins over both at read time.
function onObjectLeaveContainer(container, obj)
    local ok = pcall(function()
        if obj == nil or obj.name ~= "Custom_Token" then return end
        local redSign = redHalfSign()
        if redSign == nil then return end
        local cz = container.getPosition().z
        if cz == 0 then return end
        markerSide[obj.getGUID()] = ((cz >= 0 and 1 or -1) == redSign) and "red" or "blue"
    end)
    return ok
end

-- Team = GMNotes "Red"/"Blue" (the mod's own convention, set via our Tag buttons or
-- LCT's dormant hotkeys), else who-dropped-it (above), else nil → grey in the
-- viewer until claimed. NO table-half guessing: infiltrators deploy in the wrong
-- half, and a wrong colour is worse than an honest grey (Fendi, 2026-07-05).
function modelTeam(obj)
    local gm = obj.getGMNotes()
    if gm == "Red" then return "red" end
    if gm == "Blue" then return "blue" end
    return teamByGuid[obj.getGUID()]
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

-- Mesh key joins snapshots to the server's geometry cache. Identity = the whole
-- ASSEMBLY: parent disc id + child sculpt id ("p-c"), else parent id alone.
-- Neither half is unique by itself: ForceOrg reuses one 32mm disc under entire
-- factions AND reuses sculpt files across models (its Storm Speeder is a marine
-- sculpt scaled 3.9x on a 90mm disc). Needs getData(), so cache per GUID:
-- one serialization per model per session. Asset bundles have no mesh → nil.
guidKey = guidKey or {}

function modelKey(obj)
    local g = obj.getGUID()
    local cached = guidKey[g]
    if cached ~= nil then
        if cached == false then return nil end
        return cached
    end
    local key = nil
    local ok, dat = pcall(function() return obj.getData() end)
    if ok and dat and dat.CustomMesh and dat.CustomMesh.MeshURL then
        local pid = string.match(dat.CustomMesh.MeshURL, "/ugc/(%d+)/")
        local ch = dat.ChildObjects and dat.ChildObjects[1]
        local curl = ch and ch.CustomMesh and ch.CustomMesh.MeshURL
        local cid = curl and string.match(curl, "/ugc/(%d+)/")
        if pid then key = cid and (pid .. "-" .. cid) or pid end
    end
    guidKey[g] = key or false
    return key
end

-- Viewer contract per model: {n=clean name, x, z (inches), r=rotY, b=[w,h] bounds
-- oval (inches — measured base/guide lookup in the viewer beats this; b is the
-- last-resort fallback), t=team, w=wounds "cur/max", u=yellowscribe unit id,
-- g=mesh key, s=instance scale when not 1}.
-- Second return: a movement signature (GUID:pos:rot per model, sorted so object
-- iteration order can't flap it) — any actual move triggers a snapshot within one
-- 5s poll instead of waiting for the 60s heartbeat.
-- Whose turn is running (LCT Start Menu's currentTurn, flipped by the Pass Turn
-- HUD button) + the phase name. Drives the replay's per-player chess clock.
phaseNames = phaseNames or nil

function readTurn()
    local sm = findObj(START_MENU)
    if sm == nil then return nil end
    local t = sm.getVar("currentTurn")
    if t ~= "Red" and t ~= "Blue" then return nil end
    local out = {active = string.lower(t)}
    if phaseNames == nil then
        local ok, ph = pcall(function() return sm.getTable("phases") end)
        phaseNames = (ok and type(ph) == "table") and ph or false
    end
    if phaseNames then
        local idx = tonumber(sm.getVar("currentPhase"))
        local name = idx and phaseNames[idx]
        if type(name) == "string" then out.phase = name end
    end
    return out
end

-- Status markers: unlocked Custom_Tokens on the mat. Side = "(Red)"/"(Blue)" name
-- suffix > rack provenance / drop claim > nil (grey).
MARKER_EXCLUDE = {"command points", "gain cp", "interactive"}

function stripTags(s)
    return string.gsub(string.gsub(string.gsub(s or "", "%[[^%]]-%]", ""), "^%s+", ""), "%s+$", "")
end

function markerTeam(obj, name)
    local m = string.match(name, "%((Red)%)$") or string.match(name, "%((Blue)%)$")
    if m then return string.lower(m) end
    return markerSide[obj.getGUID()]
end

-- Reserves / Transports: LCT parks undeployed units on a locked "Reinforcements
-- and Reserves Board" behind each table edge. Models sitting there are captured
-- with v=1 (viewer re-packs them into side gutters). Which board = whose side
-- (z-sign vs red's hand), and sitting on your own board claims the unit sticky —
-- transports arrive on the battlefield pre-attributed. Rects cached once found;
-- re-scanned while sides are unknown (nobody seated as Red yet).
RESERVE_BOARD = "reinforcements and reserves"

function findReserveRects()
    local rects = {}
    local redSign = redHalfSign()
    for _, obj in ipairs(getAllObjects()) do
        if obj.name == "Custom_Token" and obj.getLock()
                and string.find(string.lower(obj.getName() or ""), RESERVE_BOARD, 1, true) then
            local ok, b = pcall(function() return obj.getBounds() end)
            if ok and b and b.center.z ~= 0 then
                local side = nil
                if redSign ~= nil then
                    side = ((b.center.z >= 0 and 1 or -1) == redSign) and "red" or "blue"
                end
                table.insert(rects, {
                    x1 = b.center.x - b.size.x / 2, x2 = b.center.x + b.size.x / 2,
                    z1 = b.center.z - b.size.z / 2, z2 = b.center.z + b.size.z / 2,
                    side = side,
                })
            end
        end
    end
    return rects
end

function reserveRectAt(p)
    for _, r in ipairs(reserveRects or {}) do
        if p.x >= r.x1 and p.x <= r.x2 and p.z >= r.z1 and p.z <= r.z2 then return r end
    end
    return nil
end

function readModels()
    local models = {}
    local markers = {}
    local sigParts = {}
    local guids = {}
    local rescan = reserveRects == nil or #reserveRects == 0
    if not rescan then
        for _, r in ipairs(reserveRects) do
            if r.side == nil then rescan = true end
        end
    end
    if rescan then reserveRects = findReserveRects() end
    for _, obj in ipairs(getAllObjects()) do
        if obj.name == "Custom_Token" and not obj.getLock() then
            local p = obj.getPosition()
            if math.abs(p.x) <= MAT_X and math.abs(p.z) <= MAT_Z then
                local name = stripTags(obj.getName())
                local lower = string.lower(name)
                local excluded = name == ""
                for _, s in ipairs(MARKER_EXCLUDE) do
                    if string.find(lower, s, 1, true) then excluded = true end
                end
                if not excluded and #markers < 100 then
                    local b = obj.getBoundsNormalized().size
                    table.insert(sigParts, string.format("%s:%.1f:%.1f", obj.getGUID(), p.x, p.z))
                    table.insert(markers, {
                        n = name,
                        x = round(p.x, 2), z = round(p.z, 2),
                        b = {round(b.x, 1), round(b.z, 1)},
                        t = markerTeam(obj, name),
                    })
                end
            end
        end
        if obj ~= self and not obj.getLock() and isModelType(obj.name) and not isExcluded(obj) then
            local p = obj.getPosition()
            local onMat = math.abs(p.x) <= MAT_X and math.abs(p.z) <= MAT_Z
            local res = nil
            if not onMat then res = reserveRectAt(p) end
            if onMat or res then
                if res and res.side and teamByGuid[obj.getGUID()] == nil then
                    teamByGuid[obj.getGUID()] = res.side
                end
                local b = obj.getBoundsNormalized().size
                local name, wounds = cleanName(obj.getName())
                local gk = modelKey(obj)
                local sc = obj.getScale().x
                if gk and not sentGeom[gk] then
                    sentGeom[gk] = true
                    table.insert(geomQueue, obj.getGUID())
                end
                local rot = obj.getRotation().y
                table.insert(sigParts, string.format("%s:%.1f:%.1f:%.0f", obj.getGUID(), p.x, p.z, rot))
                table.insert(models, {
                    n = name,
                    x = round(p.x, 2), z = round(p.z, 2),
                    r = round(rot, 1),
                    b = {round(b.x, 1), round(b.z, 1)},
                    t = modelTeam(obj),
                    w = wounds,
                    u = unitId(obj),
                    g = gk,
                    s = math.abs(sc - 1) > 0.01 and round(sc, 2) or nil,
                    v = res and 1 or nil,
                })
                table.insert(guids, obj.getGUID())
            end
        end
    end
    -- One claimed model colours its whole yellowscribe unit (dropping a single
    -- infiltrator claims the squad); persists so the claim is sticky.
    local unitTeam = {}
    for _, m in ipairs(models) do
        if m.u and m.t and unitTeam[m.u] == nil then unitTeam[m.u] = m.t end
    end
    for i, m in ipairs(models) do
        if m.u and m.t == nil and unitTeam[m.u] ~= nil then
            m.t = unitTeam[m.u]
            teamByGuid[guids[i]] = m.t
        end
    end
    table.sort(sigParts)
    return models, table.concat(sigParts, "|"), markers
end

-- One-time geometry post per unique sculpt: ship the mesh URLs + child transform to
-- the server, which downloads and measures OFF the table. Max 2 per 5s poll tick
-- (24/min, under the server's 30/min geom rate limit) so a 100-model deployment
-- never bunches WebRequests.
function pumpGeomQueue()
    local n = 0
    while #geomQueue > 0 and n < 2 do
        local obj = getObjectFromGUID(table.remove(geomQueue, 1))
        if obj ~= nil and not obj.isDestroyed() then
            local ok, dat = pcall(function() return obj.getData() end)
            if ok and dat and dat.CustomMesh and dat.CustomMesh.MeshURL then
                local body = {
                    key = modelKey(obj),
                    name = (cleanName(obj.getName())),
                    mesh = dat.CustomMesh.MeshURL,
                }
                local ch = dat.ChildObjects and dat.ChildObjects[1]
                if ch and ch.CustomMesh and ch.CustomMesh.MeshURL and ch.Transform then
                    body.child_mesh = ch.CustomMesh.MeshURL
                    body.child_rot = ch.Transform.rotY or 0
                    body.child_x = ch.Transform.posX or 0
                    body.child_z = ch.Transform.posZ or 0
                    body.child_scale = ch.Transform.scaleX or 1
                end
                if body.key then postJson("/api/geom", body, function() end) end
            end
        end
        n = n + 1
    end
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
        publishLink()
        log("recording — replay link in the Notebook (top of screen)", TEAL)
        doSnapshot(nil)
    end)
end

function doSnapshot(markLabel, models, markers)
    if sessionSlug == nil then return end
    local ok, body = pcall(function()
        local scores = readScores() or {}
        scores.turn = readTurn()  -- rides in the scores blob; no schema change
        if models == nil then
            local m, _, mk = readModels()
            models, markers = m, mk
        end
        return {
            slug = sessionSlug,
            round = readCounter(ROUND_COUNTER),
            mark = markLabel,
            scores = scores,
            cards = readCards(),
            models = models,
            markers = markers or {},
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

function computeSig(modelsSig)
    local sheet = findObj(SCORESHEET)
    local sheetState = (sheet and sheet.script_state) or ""
    local turn = readTurn()
    return string.format("%s|%d|%d|%d|%d|%d|%s|%s|%s|%s", sheetState,
        readCounter(ROUND_COUNTER), readCounter(TURN_COUNTERS.red), readCounter(TURN_COUNTERS.blue),
        readCounter(CP_COUNTERS.red), readCounter(CP_COUNTERS.blue),
        turn and (turn.active .. (turn.phase or "")) or "",
        table.concat(readSecNames("red"), ","), table.concat(readSecNames("blue"), ","),
        modelsSig or "")
end

function onPollTick()
    if dedupeCheck() then return end
    if sessionSlug == nil then
        tryStartSession()
        return
    end
    local ok, models, msig, markers = pcall(readModels)
    if not ok then return end
    local ok2, sig = pcall(computeSig, msig)
    if not ok2 then return end
    if sig ~= lastSig or (os.time() - lastPostAt) >= FORCE_POST_SECONDS then
        lastSig = sig
        doSnapshot(nil, models, markers)
    end
    pcall(pumpGeomQueue)
end

---------------------------------------------------------------------------
-- Replay link (no buttons — the token is fully automatic; Fendi, 2026-07-05)
---------------------------------------------------------------------------
-- Chat links aren't clickable in TTS; the Notebook's text is selectable/copyable,
-- so the replay URL lives there (plus the welcome page for bookmark-and-click).
function publishLink()
    if sessionPath == nil then return end
    local body = "Replay / live scoreboard for THIS game:\n" .. SERVER_URL .. sessionPath
        .. "\n\n(select the URL above and Ctrl+C)\n\nAll replays: " .. SERVER_URL
    local ok, tabs = pcall(getNotebookTabs)
    if ok and tabs ~= nil then
        for _, t in ipairs(tabs) do
            if t.title == "Snapshotbot" then
                pcall(editNotebookTab, {index = t.index, title = "Snapshotbot", body = body})
                return
            end
        end
    end
    pcall(addNotebookTab, {title = "Snapshotbot", body = body})
end

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
    return JSON.encode({slug = sessionSlug, path = sessionPath, teams = teamByGuid,
                        msides = markerSide})
end

function onLoad(saved)
    if saved ~= nil and saved ~= "" then
        local ok, st = pcall(JSON.decode, saved)
        if ok and st then
            sessionSlug = st.slug
            sessionPath = st.path
            teamByGuid = st.teams or {}
            markerSide = st.msides or {}
        end
    end
    -- No buttons, no Start/Stop/End — fully automatic by design: recording begins
    -- when an LCT table is detected, the replay URL lands in chat + Notebook, teams
    -- come from who-drops-models, and the session seals itself server-side after
    -- ~90s of silence (players left TTS).
    Wait.time(function() dedupeCheck() end, 2)  -- early check, before the first poll
    pollTimerId = Wait.time(onPollTick, POLL_SECONDS, -1)
    if sessionSlug ~= nil then
        publishLink()
        log("resumed session — replay link in the Notebook", TEAL)
    end
end
