--[[ Snapshotbot 2.0 — LCT game capture token.
     Distributed as a Saved Object (no manual pasting). Reads LCT state via TTS APIs
     and POSTs JSON to the Railway service. Models-only tracking: terrain/objectives
     come from the server's layout library, never captured here.
     All cross-object reads happen inside Wait.time callbacks wrapped in pcall
     (never onUpdate — TTS crashes on cross-object access there). --]]

SERVER_URL = "https://snapshotbot-production.up.railway.app"
TOKEN_VERSION = "dev"   -- stamped by build_saved_object.py (date + code hash)
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
markerBagName = markerBagName or {}  -- marker GUID -> dispensing bag name when it differs
sentGeom = sentGeom or {}      -- mesh keys already posted this session
geomQueue = geomQueue or {}    -- model GUIDs awaiting a one-time geometry post
sentRoster = sentRoster or {}  -- "unit|model name" pairs whose datasheet is already posted
rosterQueue = rosterQueue or {} -- model GUIDs awaiting a one-time datasheet post

function log(msg, color)
    broadcastToAll("[Snapshotbot] " .. tostring(msg), color or GREY)
end

-- Black box (Fendi, 2026-07-24): a rolling ring of the token's own notable events,
-- posted to the server the moment the token is removed so a recording that ended
-- unexpectedly can be diagnosed after the fact. In-memory only until then — never
-- a WebRequest per line — and globals so it survives Execute-Lua re-pushes in dev.
bornAt = bornAt or os.time()
traceRing = traceRing or {}

function trace(msg)
    traceRing[#traceRing + 1] = {t = os.time(), m = tostring(msg)}
    while #traceRing > 40 do table.remove(traceRing, 1) end
end

-- Every error lands in the server's Railway logs (visible with `railway logs`),
-- tagged with session + token GUID. Chat only sees one red line per minute.
lastErrBroadcast = lastErrBroadcast or 0

function remoteLog(level, msg)
    trace(level .. ": " .. tostring(msg))  -- errors/warns are the heart of the black box
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
                -- surface the server's reason ("at capacity ...") when it sent one
                local why = "HTTP " .. tostring(req.response_code)
                local okE, parsed = pcall(JSON.decode, req.text or "")
                if okE and parsed and parsed.error then why = parsed.error end
                if cb then cb(nil, why) end
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

function readCards(loose)
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
    -- Loose face-up cards are collected by readModels' single table sweep and
    -- passed in — this function must never sweep the table itself.
    cards.loose = loose or {}
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
    if internalName == nil then return false end
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

-- Duplicate tokens announce themselves by spawning: check right then instead of
-- sweeping the whole table every poll tick (a slow 1/min tick check remains as
-- the net). 1s delay lets the newcomer finish onLoad so the election sees its vars.
function onObjectSpawn(obj)
    pcall(function()
        if obj ~= self and obj.getName() == "Snapshotbot" then
            Wait.time(function() pcall(dedupeCheck) end, 1)
        end
    end)
end

-- Status markers are dispensed from per-side racks of infinite bags: leaving a
-- container on red's half means red placed it. More precise than the drop claim,
-- so it overwrites; the "(Red)"/"(Blue)" name suffix wins over both at read time.
-- The bag also names the marker truthfully where the token nickname lies (LCT's
-- Objective Secured Red/Blue bags dispense tokens NICKNAMED "Action"), so a
-- differing bag name is remembered as origin provenance. First claim sticky;
-- "" = witnessed but bag agrees, so don't re-check on later dispenses.
function onObjectLeaveContainer(container, obj)
    local ok = pcall(function()
        if obj == nil or obj.name ~= "Custom_Token" then return end
        local g = obj.getGUID()
        if markerBagName[g] == nil then
            local bn = stripTags(container.getName())
            if bn ~= "" and string.lower(bn) ~= string.lower(stripTags(obj.getName())) then
                markerBagName[g] = bn
            else
                markerBagName[g] = ""
            end
        end
        local redSign = redHalfSign()
        if redSign == nil then return end
        local cz = container.getPosition().z
        if cz == 0 then return end
        markerSide[g] = ((cz >= 0 and 1 or -1) == redSign) and "red" or "blue"
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

-- The 5s sweep re-parses the same nicknames thousands of times per game; memoize
-- by the raw string (wound counters change the key, so hits stay correct). The
-- cap only guards a pathological table from growing the memo forever.
nameMemo = nameMemo or {}
nameMemoN = nameMemoN or 0

function memoName(raw)
    local hit = nameMemo[raw]
    if hit then return hit[1], hit[2] end
    if nameMemoN >= 4096 then nameMemo = {}; nameMemoN = 0 end
    local n, w = cleanName(raw)
    nameMemo[raw] = {n, w}
    nameMemoN = nameMemoN + 1
    return n, w
end

-- Facts that can't change while an object lives (bounds size, yellowscribe unit
-- tag, spawn scale, nickname-substring exclusion): each is a Lua->C# API call,
-- so pay them once per object, not on every poll tick.
modelFacts = modelFacts or {}
markerBounds = markerBounds or {}
markerImgIds = markerImgIds or {}  -- marker GUID -> custom-image short id ("" = none)

function modelFactsFor(obj, g)
    local f = modelFacts[g]
    if f == nil then
        local b = obj.getBoundsNormalized().size
        local sc = obj.getScale().x
        f = {
            b = {round(b.x, 1), round(b.z, 1)},
            u = unitId(obj),
            s = math.abs(sc - 1) > 0.01 and round(sc, 2) or nil,
            ex = isExcluded(obj),
        }
        modelFacts[g] = f
    end
    return f
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

-- Assembly identity + geometry must cover the WHOLE descendant tree: flight-stand
-- vehicles carry the hull as a SECOND child (Raider = disc + stand + hull-with-
-- grandchild), so keying parent-child[1] collided different vehicles onto one key
-- and baked stand-only silhouettes (found live: session 40 Raider+Venom = circles).
MAX_GEOM_IDS = 6   -- descendant cap, mirrored in server validation + precrunch

function childSpecs(list, ids, depth)
    local out = nil
    for _, ch in ipairs(list or {}) do
        if #ids >= MAX_GEOM_IDS or depth > 3 then break end
        local curl = ch.CustomMesh and ch.CustomMesh.MeshURL
        local cid = curl and string.match(curl, "/ugc/(%d+)/")
        if cid and ch.Transform then
            table.insert(ids, cid)
            local node = {mesh = curl, rot = ch.Transform.rotY or 0,
                          x = ch.Transform.posX or 0, z = ch.Transform.posZ or 0,
                          scale = ch.Transform.scaleX or 1}
            node.children = childSpecs(ch.ChildObjects, ids, depth + 1)
            out = out or {}
            table.insert(out, node)
        end
    end
    return out
end

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
        if pid then
            local ids = {}
            childSpecs(dat.ChildObjects, ids, 1)
            key = pid
            for _, cid in ipairs(ids) do key = key .. "-" .. cid end
        end
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

-- The ONLY per-tick table sweep: models, status markers AND loose face-up cards
-- come out of one getAllObjects pass. When called from the poll coroutine
-- (chunked=true) it yields a frame every CHUNK objects, so a big table never
-- stalls a single frame — the 5s cadence hides a sweep spread over ~5 frames.
CHUNK = 50

function readModels(chunked)
    local models = {}
    local markers = {}
    local loose = {}
    local sigParts = {}
    local guids = {}
    -- The reserves board isn't fixed: LCT's "Move Board" button toggles it between
    -- z ~84 and z ~59.5 (staying locked). A cached rect then misses every model on
    -- the board (off-mat + outside the rect = dropped entirely, so reserves vanish
    -- until it toggles back). Re-scan every ~6 ticks so the rect follows the board.
    local rescan = reserveRects == nil or #reserveRects == 0 or tickCount % 6 == 0
    if not rescan then
        for _, r in ipairs(reserveRects) do
            if r.side == nil then rescan = true end
        end
    end
    if rescan then reserveRects = findReserveRects() end
    -- Hand cards are hidden info and must not leak into a shared replay.
    local inHand = {}
    for _, color in ipairs(getSeatedPlayers()) do
        local ok, held = pcall(function() return Player[color].getHandObjects() end)
        if ok and held then
            for _, h in ipairs(held) do inHand[h.getGUID()] = true end
        end
    end
    local redSign = redHalfSign()
    for i, obj in ipairs(getAllObjects()) do
        if chunked and i % CHUNK == 0 then coroutine.yield(0) end
        -- getAllObjects() is captured once, but the chunked sweep yields across
        -- frames; an object picked up or deleted during a yield leaves a destroyed
        -- reference. Touching it throws a host NRE that escapes pcall and aborts
        -- pollCo (Fendi, 2026-07-09) — skip vanished objects so the sweep finishes.
        -- (No goto here: MoonSharp rejects a jump past a local declaration, so
        -- vanished objects instead leave tname nil, which matches no branch.)
        local tname = nil
        if obj ~= nil and not obj.isDestroyed() then tname = obj.name end
        if tname == "Custom_Token" and not obj.getLock() then
            local p = obj.getPosition()
            if math.abs(p.x) <= MAT_X and math.abs(p.z) <= MAT_Z then
                local name = stripTags(obj.getName())
                local lower = string.lower(name)
                local excluded = name == ""
                for _, s in ipairs(MARKER_EXCLUDE) do
                    if string.find(lower, s, 1, true) then excluded = true end
                end
                if not excluded and #markers < 100 then
                    local g = obj.getGUID()
                    local b = markerBounds[g]
                    if b == nil then
                        local bs = obj.getBoundsNormalized().size
                        b = {round(bs.x, 1), round(bs.z, 1)}
                        markerBounds[g] = b
                    end
                    -- Image identity: LCT misnames some tokens (the Objective
                    -- Secured Red/Blue bags dispense tokens NICKNAMED "Action"),
                    -- so the viewer classifies by image first, name as fallback.
                    -- Short id = last 12 chars of the URL's final path segment.
                    local iid = markerImgIds[g]
                    if iid == nil then
                        local okI, url = pcall(function()
                            return obj.getCustomObject().image
                        end)
                        iid = ""
                        if okI and type(url) == "string" then
                            local seg = string.match(url, "(%w+)/*$")
                            if seg then iid = string.sub(string.lower(seg), -12) end
                        end
                        markerImgIds[g] = iid
                    end
                    local bn = markerBagName[g]
                    table.insert(sigParts, string.format("%s:%.1f:%.1f", g, p.x, p.z))
                    table.insert(markers, {
                        n = name,
                        x = round(p.x, 2), z = round(p.z, 2),
                        b = b,
                        t = markerTeam(obj, name),
                        i = iid ~= "" and iid or nil,
                        bn = (bn ~= nil and bn ~= "") and bn or nil,
                    })
                end
            end
        elseif (tname == "Card" or tname == "CardCustom") and #loose < 60 then
            local g = obj.getGUID()
            if not inHand[g] then
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
        elseif obj ~= self and isModelType(tname) and not obj.getLock() then
            local g = obj.getGUID()
            local facts = modelFactsFor(obj, g)
            if not facts.ex then
                local p = obj.getPosition()
                local onMat = math.abs(p.x) <= MAT_X and math.abs(p.z) <= MAT_Z
                local res = nil
                if not onMat then res = reserveRectAt(p) end
                if onMat or res then
                    if res and res.side and teamByGuid[g] == nil then
                        teamByGuid[g] = res.side
                    end
                    local name, wounds = memoName(obj.getName())
                    local gk = modelKey(obj)
                    if gk and not sentGeom[gk] then
                        sentGeom[gk] = true
                        table.insert(geomQueue, g)
                    end
                    -- One datasheet post per (unit, model name): that pair IS the wargear
                    -- choice, so 9 halberd Sacresants cost one post, not nine. Only the
                    -- getDescription/getLuaScript calls in the pump are expensive — this is
                    -- a table lookup, safe in the hot loop.
                    if facts.u and name then
                        local rk = facts.u .. "|" .. name
                        if not sentRoster[rk] then
                            sentRoster[rk] = true
                            table.insert(rosterQueue, g)
                        end
                    end
                    local rot = obj.getRotation().y
                    table.insert(sigParts, string.format("%s:%.1f:%.1f:%.0f", g, p.x, p.z, rot))
                    table.insert(models, {
                        n = name,
                        x = round(p.x, 2), z = round(p.z, 2),
                        r = round(rot, 1),
                        b = facts.b,
                        t = modelTeam(obj),
                        w = wounds,
                        u = facts.u,
                        g = gk,
                        s = facts.s,
                        v = res and 1 or nil,
                        -- Position WITHIN the reserves board, normalised 0..1. The board is
                        -- printed with labelled zones — DEEP STRIKE / STRATEGIC RESERVES /
                        -- TRANSPORTS (8 numbered slots) — so where a model sits IS its reserve
                        -- category, and which transport it rides. Sent raw (not classified) so
                        -- the band thresholds live server-side and can be corrected from a
                        -- recording without rebuilding and re-spawning the token.
                        rx = res and round((p.x - res.x1) / math.max(res.x2 - res.x1, 0.001), 3) or nil,
                        rz = res and round((p.z - res.z1) / math.max(res.z2 - res.z1, 0.001), 3) or nil,
                    })
                    table.insert(guids, g)
                end
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
    return models, table.concat(sigParts, "|"), markers, loose
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
                    children = childSpecs(dat.ChildObjects, {}, 1),
                }
                if body.key then postJson("/api/geom", body, function() end) end
            end
        end
        n = n + 1
    end
end

-- One-time datasheet post per (unit, model name). Yellowscribe puts a colour-tagged
-- statline + weapon profiles + abilities in every model's Description, and the unit's
-- `local unitData = {...}` table at the head of the LEADER model's script (the ~54KB
-- shared boilerplate that follows starts at "local scriptingFunctions" — cut there, we
-- want the list data, not the library). Model names alone pin the wargear CHOICE but not
-- its profile, which forced post-game analysis to guess from BSData's full option menu.
-- 1 per tick: these payloads are far bigger than a geom spec.
-- Reading is cheap, POSTing is not: one row per 5s tick meant a 27-row army took ~2.5
-- minutes to land. Rows are BATCHED into a single request instead (~4 requests, ~20s), and
-- the one genuinely expensive host call — getLuaScript() on a leader returns the ~54KB
-- Yellowscribe boilerplate — is capped per tick and skipped entirely on non-leaders, which
-- Yellowscribe tags for us. Nothing here is urgent: the roster cannot change mid-game.
ROSTER_ROWS_PER_TICK = 8
ROSTER_SCRIPTS_PER_TICK = 2

function isLeaderModel(obj)
    for _, t in ipairs(obj.getTags() or {}) do
        if t == "leaderModel" then return true end
    end
    return false
end

function pumpRosterQueue()
    if sessionSlug == nil or #rosterQueue == 0 then return end
    local rows, scripts, n = {}, 0, 0
    while #rosterQueue > 0 and n < ROSTER_ROWS_PER_TICK do
        n = n + 1
        local obj = getObjectFromGUID(table.remove(rosterQueue, 1))
        if obj ~= nil and not obj.isDestroyed() then
            pcall(function()
                local name = (cleanName(obj.getName()))
                local u = unitId(obj)
                if u == nil or name == "" then return end
                local row = {
                    u = u, n = name, t = modelTeam(obj),
                    d = string.sub(obj.getDescription() or "", 1, 8000),
                }
                -- Only a leader carries unitData; everyone else's script is empty, so don't
                -- pay for the call. Cap the big reads so one tick never stalls a frame.
                if scripts < ROSTER_SCRIPTS_PER_TICK and isLeaderModel(obj) then
                    scripts = scripts + 1
                    local src = obj.getLuaScript() or ""
                    if src ~= "" then
                        local cut = string.find(src, "local scriptingFunctions", 1, true)
                        if cut then src = string.sub(src, 1, cut - 1) end
                        if string.find(src, "unitData", 1, true) then
                            row.data = string.sub(src, 1, 60000)
                            -- unitName is the DATASHEET name ("Celestian Sacresants"); model
                            -- nicknames are per-model wargear variants ("Celestian Sacresant
                            -- (Anointed Halberd)"), so deriving a unit label from them gets
                            -- both the name and the grouping wrong. Take it from the source.
                            row.un = string.match(src, 'unitName%s*=%s*"([^"]*)"')
                            row.fkw = string.match(src, 'factionKeywords%s*=%s*"([^"]*)"')
                            -- factionKeywords is matched first, so anchor keywords to line start
                            row.kw = string.match(src, '\n%s*keywords%s*=%s*"([^"]*)"')
                        end
                    end
                elseif isLeaderModel(obj) then
                    table.insert(rosterQueue, obj.getGUID())  -- retry next tick, still owed
                    return
                end
                table.insert(rows, row)
            end)
        end
    end
    if #rows > 0 then
        postJson("/api/roster", {slug = sessionSlug, rows = rows}, function() end)
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
                    trace("dedupe: yielding to token " .. tostring(obj.getGUID()))
                    quietDestroy = true  -- expected removal: no recording-stopped alarm
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
            local why = (resp and resp.error) or err
            logError("session start failed (" .. tostring(why) .. ") — retrying later")
            return
        end
        sessionSlug = resp.slug
        sessionPath = resp.path
        trace((resp.resumed and "adopted session " or "session started ") .. tostring(resp.slug))
        publishLink()
        log("recording — replay link in the Notebook (top of screen)", TEAL)
        doSnapshot(nil)
    end)
end

function doSnapshot(markLabel, models, markers, loose)
    if sessionSlug == nil then return end
    local ok, body = pcall(function()
        local scores = readScores() or {}
        scores.turn = readTurn()  -- rides in the scores blob; no schema change
        if models == nil then
            local m, _, mk, lc = readModels()
            models, markers, loose = m, mk, lc
        end
        return {
            slug = sessionSlug,
            round = readCounter(ROUND_COUNTER),
            mark = markLabel,
            scores = scores,
            cards = readCards(loose),
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

-- Change detection only needs the RAW script_state strings — decoding five
-- counters' JSON every tick just to re-encode them into a signature was waste.
-- The real decode happens in doSnapshot, only when something actually changed.
function counterRaw(ref)
    local o = findObj(ref)
    return (o and o.script_state) or ""
end

function computeSig(modelsSig)
    local sheet = findObj(SCORESHEET)
    local sheetState = (sheet and sheet.script_state) or ""
    local turn = readTurn()
    return string.format("%s|%s|%s|%s|%s|%s|%s|%s|%s|%s", sheetState,
        counterRaw(ROUND_COUNTER), counterRaw(TURN_COUNTERS.red), counterRaw(TURN_COUNTERS.blue),
        counterRaw(CP_COUNTERS.red), counterRaw(CP_COUNTERS.blue),
        turn and (turn.active .. (turn.phase or "")) or "",
        table.concat(readSecNames("red"), ","), table.concat(readSecNames("blue"), ","),
        modelsSig or "")
end

-- The poll body runs as a TTS coroutine so the table sweep can yield a frame
-- every CHUNK objects — big tables cost several invisible frames instead of one
-- visible hitch. TTS's single Lua thread means everyone shares our frame budget.
polling = polling or false
tickCount = tickCount or 0
lastTickAt = nil
pollStartedAt = pollStartedAt or 0

function onPollTick()
    lastTickAt = os.time()
    if polling then return end
    polling = true
    pollStartedAt = os.time()
    startLuaCoroutine(self, "pollCo")
end

-- Status glow (Fendi, 2026-07-09): the token's highlight IS the health indicator —
-- glanceable at the table, no Discord, no server. Green = recording (a post
-- landed <90s ago), yellow = LCT table seen but no session yet (starting /
-- capacity retry / first post pending), red = session live but posts failing,
-- off = not an LCT table. highlightOn tints the outline only — Fendi's Gnarlmaw
-- paint job stays untouched.
glowState = glowState or "?"
lastGlowAt = lastGlowAt or 0

function updateGlow(now)
    local s
    if sessionSlug ~= nil then
        s = (lastPostAt > 0 and now - lastPostAt < 90) and "ok"
            or (lastPostAt == 0 and "wait" or "stale")
    elseif getObjectFromGUID(SCORESHEET.guid) ~= nil then
        s = "wait"
    else
        s = nil
    end
    if s ~= glowState then
        glowState = s
        if s == "ok" then self.highlightOn({0.10, 0.85, 0.25})
        elseif s == "stale" then self.highlightOn({0.95, 0.15, 0.15})
        elseif s == "wait" then self.highlightOn({0.95, 0.75, 0.10})
        else self.highlightOff() end
    end
end

-- Watchdog (j_efPv9t died silently mid-game, 2026-07-08): Wait.stopAll() from ANY
-- cohabiting script kills our repeating timer, and a poll coroutine TTS never
-- resumes leaves `polling` stuck true — pcall catches neither. onUpdate fires
-- every frame straight from the engine, so it survives both; body stays trivial.
function onUpdate()
    local now = os.time()
    if lastTickAt == nil then lastTickAt = now return end
    if now - lastGlowAt >= 2 then
        lastGlowAt = now
        pcall(updateGlow, now)
    end
    if polling and now - pollStartedAt > 15 then
        polling = false
        pcall(remoteLog, "warn", "watchdog: stuck poll coroutine cleared")
    end
    if now - lastTickAt > 30 then
        lastTickAt = now  -- moves even if re-arm fails: never error every frame
        pcall(function()
            if pollTimerId then pcall(Wait.stop, pollTimerId) end
            pollTimerId = Wait.time(onPollTick, POLL_SECONDS, -1)
            remoteLog("warn", "watchdog: poll loop re-armed")
        end)
    end
end

function pollCo()
    local ok, err = pcall(function()
        tickCount = tickCount + 1
        if tickCount % 24 == 0 then  -- ~2min heartbeat so the black box shows a timeline
            trace("alive t=" .. tickCount .. (sessionSlug ~= nil
                and (" since_post=" .. (lastPostAt > 0 and (os.time() - lastPostAt) or -1) .. "s")
                or " no-session"))
        end
        if tickCount % 12 == 1 and dedupeCheck() then return end  -- ~1/min belt-and-braces
        if sessionSlug == nil then
            tryStartSession()
            return
        end
        local models, msig, markers, loose = readModels(true)
        local sig = computeSig(msig)
        if sig ~= lastSig or (os.time() - lastPostAt) >= FORCE_POST_SECONDS then
            lastSig = sig
            doSnapshot(nil, models, markers, loose)
        end
        pumpGeomQueue()
        pumpRosterQueue()
    end)
    polling = false
    if not ok then remoteLog("error", "poll failed: " .. tostring(err)) end
    return 1
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
-- Downloaded tokens age on people's disks: ask the server (which always serves
-- the latest build) whether this one is stale, and say so in chat once per load.
function checkVersion()
    pcall(function()
        WebRequest.get(SERVER_URL .. "/api/version", function(req)
            pcall(function()
                if req.is_error or req.response_code >= 400 then return end
                local ok, d = pcall(JSON.decode, req.text)
                if ok and d and d.version and d.version ~= TOKEN_VERSION then
                    log("this token is version " .. TOKEN_VERSION .. "; " .. d.version
                        .. " is out — redownload from " .. SERVER_URL, RED)
                end
            end)
        end)
    end)
end

function onDestroy()
    -- Manual deletion is a legitimate way to stop recording: kill the timers,
    -- leave the session to seal itself server-side (~90s of silence). But an
    -- ACCIDENTAL delete (or bagging) mid-game must not be silent — shout so the
    -- table knows recording stopped; a respawned token re-adopts the session.
    if pollTimerId then pcall(Wait.stop, pollTimerId) end
    if sessionSlug ~= nil and not quietDestroy then
        pcall(broadcastToAll,
            "[Snapshotbot] token removed — recording STOPPED. Respawn it to resume this game.",
            RED)
    end
    -- Submit the black box. Fire-and-forget: the request is handed to TTS's network
    -- layer before this object finishes tearing down, and the callback is a no-op so
    -- nothing runs on the dead script. Invisible to the table — troubleshooting only.
    pcall(function()
        local now = os.time()
        trace("onDestroy (" .. (quietDestroy and "quiet" or "removed") .. ")")
        local body = {
            slug = sessionSlug,
            guid = self.getGUID(),
            reason = quietDestroy and "quiet" or "removed",
            version = TOKEN_VERSION,
            payload = {
                uptime_s = now - (bornAt or now),
                since_last_post_s = lastPostAt > 0 and (now - lastPostAt) or nil,
                had_session = sessionSlug ~= nil,
                path = sessionPath,
                ticks = tickCount,
                geom_pending = #geomQueue,
                roster_pending = #rosterQueue,
                trace = traceRing,
            },
        }
        WebRequest.custom(SERVER_URL .. "/api/errorlog", "POST", true, JSON.encode(body),
            {["Content-Type"] = "application/json"}, function() end)
    end)
    print("[Snapshotbot] token removed — black box posted")
end

function onSave()
    return JSON.encode({slug = sessionSlug, path = sessionPath, teams = teamByGuid,
                        msides = markerSide, mbags = markerBagName})
end

function onLoad(saved)
    if saved ~= nil and saved ~= "" then
        local ok, st = pcall(JSON.decode, saved)
        if ok and st then
            sessionSlug = st.slug
            sessionPath = st.path
            teamByGuid = st.teams or {}
            markerSide = st.msides or {}
            markerBagName = st.mbags or {}
        end
    end
    trace("onLoad v=" .. tostring(TOKEN_VERSION) .. " slug=" .. tostring(sessionSlug))
    -- No buttons, no Start/Stop/End — fully automatic by design: recording begins
    -- when an LCT table is detected, the replay URL lands in chat + Notebook, teams
    -- come from who-drops-models, and the session seals itself server-side after
    -- ~90s of silence (players left TTS).
    Wait.time(function() dedupeCheck() end, 2)  -- early check, before the first poll
    Wait.time(checkVersion, 4)
    pollTimerId = Wait.time(onPollTick, POLL_SECONDS, -1)
    if sessionSlug ~= nil then
        publishLink()
        log("resumed session — replay link in the Notebook", TEAL)
    end
end
