"""Fendi's Snapshotbot — capture.pyw

Listens for capture signals from a TTS Lua script, takes screenshots of a
calibrated screen region, strips drawing lines, and compiles a self-contained
HTML replay file when the session ends.

A small window sits on the taskbar with session controls.

Requirements (for running from source):
  pip install mss Pillow opencv-python-headless numpy dxcam
"""

import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

APP_DIR     = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
STORE_DIR   = APP_DIR / "Snapshotbot Replays"
CONFIG_FILE = APP_DIR / "replay_config.json"

_LOG_FILE = APP_DIR / "capture_debug.log"
def _log(msg: str):
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as _f:
            _f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


# ── Constants ─────────────────────────────────────────────────────────────────

VERSION = "1.2.7"  # bump on every release to confirm correct build is running

TTS_LISTEN_PORT  = 39997   # Python HTTP server; Lua WebRequest.post() sends here
TTS_SEND_PORT    = 39999   # TTS listens as SERVER; Python sends messageID 2 (customMessage) here

CAMERA_SETTLE_MS    = 1200  # initial sleep before first sample — lets TTS camera animation finish
STABILITY_POLL_MS   = 80    # ms between samples
STABILITY_MAX_POLLS = 15    # give up after this many unstable polls (~1.2 s of polling)
STABILITY_THRESHOLD = 0.995 # fraction of pixels that must match to call it stable

# ── Drawing-line exact RGB values ─────────────────────────────────────────────
# Each entry is (label, (R, G, B)).

DRAWING_COLORS_RGB = [
    ("red",    (218,  22,  22)),
    ("blue",   ( 28, 135, 255)),
    ("teal",   ( 34, 177, 155)),
    ("purple", (255,   0, 255)),
]

# Per-channel tolerance for JPEG compression rounding.  0 = pixel-perfect.
RGB_TOLERANCE  = 8   # raised from 4 — JPEG introduces more colour noise than PNG
INPAINT_RADIUS = 5

# ── Output image settings ──────────────────────────────────────────────────────
# Frames are saved as JPEG to keep replay file sizes manageable.
# FRAME_MAX_WIDTH: downscale if the captured width exceeds this (preserves aspect
#   ratio). Set to 0 to disable. 1920 is a good default for 4K monitors.
# FRAME_JPEG_QUALITY: 1-95. 85 gives excellent visual quality at ~25% of PNG size.
FRAME_MAX_WIDTH    = 1300
FRAME_JPEG_QUALITY = 85

# ── Shared app state ──────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_state = {
    "listening":   False,
    "frame_num":   0,
    "manifest":    None,
    "session_dir": None,
    "window":      None,
    "status_var":  None,
    "btn_session": None,
    "indicator":   None,
}

# ── Embedded assets ───────────────────────────────────────────────────────────

CAPTURE_BUTTON_LUA = r"""
-- =============================================================================
-- Fendi's Snapshotbot  (single-object script)
-- Attach this script to ONE object in your TTS save.
-- Right-click object -> Scripting -> paste -> Save & Play.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- CONFIG
-- -----------------------------------------------------------------------------

local TOP_DOWN_POSITION = {
    x = 0,    -- centre of battlefield (X axis)
    y = 10,   -- height above table
    z = 0,    -- centre of battlefield (Z axis)
}

local TOP_DOWN_DISTANCE = 40
local CAPTURE_INTERVAL  = 60    -- seconds between auto-captures

-- -----------------------------------------------------------------------------
-- GUIDs
-- -----------------------------------------------------------------------------

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

-- -----------------------------------------------------------------------------
-- Helpers
-- -----------------------------------------------------------------------------

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

-- -----------------------------------------------------------------------------
-- Cross-context data store -- uses self.setVar / self.getVar so data written
-- in onExternalMessage can be safely read in onUpdate and vice-versa.
-- Upvalue variables are context-isolated in TTS non-Global object scripts;
-- object vars are not.
-- -----------------------------------------------------------------------------

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

-- -----------------------------------------------------------------------------
-- State -- only PRIMITIVE upvalues; never read across contexts.
-- All inter-context communication goes through self.setVar / self.getVar.
-- -----------------------------------------------------------------------------

local recording      = false
local capturing      = false
local connected      = false
local recorder_color = nil
local recWaitID      = nil

local zoneObjs    = {}
local zonesCached = false

-- Primitive trigger flags -- written by button/Wait callbacks, read by onUpdate.
local triggerCapture  = nil    -- player_color string or nil
local triggerRecStart = nil    -- player_color string or nil
local triggerRecStop  = false  -- bool
local pendingAutoCap  = false  -- bool

-- Capture pipeline phase stored on object so it survives context switches.
-- self.setVar("phase", 0/1/2)

local BTN_CAPTURE   = 0
local BTN_REC       = 1
local BTN_CALIBRATE = 2

-- -----------------------------------------------------------------------------
-- onLoad
-- -----------------------------------------------------------------------------

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
        position       = {0, 0.1, -0.8},
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
        position       = {0, 0.1, 0},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {0.9, 0.2, 0.2},
        tooltip        = "Auto-capture every " .. CAPTURE_INTERVAL .. "s",
    })

    self.createButton({
        label          = "📐 CALIBRATE",
        click_function = "doCalibrate",
        function_owner = self,
        position       = {0, 0.1, 0.8},
        rotation       = {0, 0, 0},
        width          = 900,
        height         = 280,
        font_size      = 110,
        color          = {0.1, 0.1, 0.1},
        font_color     = {0.5, 0.8, 1.0},
        tooltip        = "Set top-down view for calibration -- then use the Python window to draw the region",
    })

    log("Loaded. Waiting for Python connection...", C.grey)
    WebRequest.post("http://127.0.0.1:39997/handshake", '{"action":"handshake"}', function(req) end)
end

-- -----------------------------------------------------------------------------
-- Zone cache + data refresh -- called ONLY from onExternalMessage
-- -----------------------------------------------------------------------------

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
    -- Called only from onExternalMessage -- safe context for cross-object calls.
    -- Cards: zone.getObjects() property reads.
    -- Scores: sheet.script_state property read (NOT sheet.call -- no ownership error).

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

        -- Read steam names directly from seated players -- plain property reads, safe everywhere.
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

-- -----------------------------------------------------------------------------
-- onUpdate -- ALL pipeline logic lives here.
-- Reads primitive trigger flags; reads/writes inter-context data via setVar/getVar.
-- -----------------------------------------------------------------------------

function onUpdate()

    -- -- Rec-stop --------------------------------------------------------------
    if triggerRecStop then
        triggerRecStop = false
        safeCall("onUpdate/rec-stop", function()
            recording = false
            if recWaitID then Wait.stop(recWaitID) recWaitID = nil end
            self.editButton({ index = BTN_REC, label = "⏺ START REC", font_color = C.red })
        end)
        return
    end

    -- -- Rec-start -------------------------------------------------------------
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

    -- -- Manual capture trigger ------------------------------------------------
    if triggerCapture and not capturing then
        local color    = triggerCapture
        triggerCapture = nil
        safeCall("onUpdate/capture-trigger", function()
            self.setVar("pendingAction",      "capture")
            self.setVar("pendingPlayerColor", color)
            self.setVar("phase", 1)
        end)
    end

    -- -- Auto-capture timer fired ----------------------------------------------
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

    -- -- Camera lock: hold top-down position while capture is in progress ---------
    -- lookAt is re-applied every frame to override any mouse movement during the
    -- 1-2s window between triggering capture and receiving capture_done.
    if capturing then
        safeCall("onUpdate/lockCamera", function()
            local pc = self.getVar("pendingPlayerColor")
            if pc then
                local p = Player[pc]
                if p and p.seated then
                    local yaw = (pc == "Red") and 180 or 0
                    p.lookAt({
                        position = TOP_DOWN_POSITION,
                        pitch    = 90,
                        yaw      = yaw,
                        distance = TOP_DOWN_DISTANCE,
                    })
                end
            end
        end)
    end

    -- -- Pipeline phase 1: position camera + request cache refresh -----------
    -- Phase advances to 2 only after onExternalMessage receives the
    -- refresh_done echo from Python, guaranteeing scores/cards are fresh.
    local phase = self.getVar("phase")
    if phase == 1 and not capturing then
        safeCall("onUpdate/phase1", function()
            local action       = self.getVar("pendingAction")
            local player_color = self.getVar("pendingPlayerColor")

            local p = Player[player_color]
            if not p or not p.seated then
                log("Player " .. tostring(player_color) .. " not seated -- skipped", C.red)
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

            -- Safety net: if Python crashes BEFORE posting /capture (during the
            -- refresh handshake), pendingRestore is never set so the phase-2
            -- fallback timer is never scheduled.  This timer covers that gap --
            -- it fires if we are still stuck at phase 1 after 15 s.
            self.setVar("phase1SafetyFired", false)
            Wait.time(function()
                if self.getVar("phase") == 1 or self.getVar("phase") == 2 then
                    if not self.getVar("pendingRestore") then
                        log("Phase-1 safety timeout -- restoring camera", C.yellow)
                        local pc = self.getVar("pendingPlayerColor")
                        local p  = pc and Player[pc]
                        if p then p.setCameraMode("ThirdPerson") end
                        self.setVar("phase", 0)
                        capturing = false
                    end
                end
            end, 15)

            -- Stay at phase 1 until Python echoes refresh_done back via
            -- onExternalMessage, which will refresh the cache then set phase 2.
            WebRequest.post("http://127.0.0.1:39997/refresh", '{"action":"refresh"}',
                function(req) end)
        end)
        return
    end

    -- -- Pipeline phase 2: send signal ----------------------------------------
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
                        -- HTTP error: capture_done will never arrive, restore now.
                        log("Signal error: " .. tostring(req.error), C.red)
                        local p = Player[player_color]
                        if p then p.setCameraMode("ThirdPerson") end
                        capturing = false
                    else
                        -- Python acknowledged. Camera restore is deferred to
                        -- onExternalMessage capture_done (fires after the screenshot
                        -- is actually saved, ~1-2s from now).
                        self.setVar("pendingRestore", player_color)
                        -- Fallback: if capture_done never arrives (e.g. Python crash),
                        -- restore camera after 10 seconds.
                        Wait.time(function()
                            if self.getVar("pendingRestore") then
                                log("Capture timeout -- restoring camera", C.yellow)
                                local pc = self.getVar("pendingRestore")
                                local p  = Player[pc]
                                if p then p.setCameraMode("ThirdPerson") end
                                self.setVar("pendingRestore", nil)
                            else
                                log("Capture timeout (no pendingRestore) -- releasing lock", C.yellow)
                            end
                            capturing = false
                        end, 10)
                    end
                end
            )
        end)
        return
    end

end

-- -----------------------------------------------------------------------------
-- Button callbacks -- write ONE primitive flag only.
-- -----------------------------------------------------------------------------

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

function doCalibrate(obj, player_color, alt_click)
    -- Positions the camera top-down so the battlefield is visible for
    -- region selection in the Python window. Does NOT capture anything.
    -- Camera returns to third-person after a short hold.
    local p = Player[player_color]
    if not p or not p.seated then
        log("Sit down first to calibrate your view", C.red)
        return
    end
    local yaw = (player_color == "Red") and 180 or 0
    p.lookAt({
        position = TOP_DOWN_POSITION,
        pitch    = 90,
        yaw      = yaw,
        distance = TOP_DOWN_DISTANCE,
    })
    log("Camera locked top-down for 15s - alt-tab to Python and click Calibrate Region now", C.yellow)
    Wait.time(function()
        p.setCameraMode("ThirdPerson")
        log("Camera released", C.grey)
    end, 15)
end

-- -----------------------------------------------------------------------------
-- Auto-capture scheduler
-- -----------------------------------------------------------------------------

function scheduleNextCapture()
    recWaitID = Wait.time(function()
        recWaitID = nil
        if not recording then return end
        pendingAutoCap = true
    end, CAPTURE_INTERVAL)
end

-- -----------------------------------------------------------------------------
-- onExternalMessage -- only safe context for cross-object calls
-- -----------------------------------------------------------------------------

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

    if data.action == "capture_done" then
        log("Done", C.green)
        local pc = self.getVar("pendingRestore")
        if pc then
            local p = Player[pc]
            if p then p.setCameraMode("ThirdPerson") end
            self.setVar("pendingRestore", nil)
        end
        capturing = false
    end

    if data.action == "reannounce" then
        connected = false
        log("Re-announce requested -- reconnecting...", C.yellow)
    end
end

"""



# ── First-run setup ───────────────────────────────────────────────────────────

def ensure_assets():
    """Write capture_button.lua on first run if missing."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    lua_path = APP_DIR / "capture_button.lua"
    if not lua_path.exists():
        lua_path.write_text(CAPTURE_BUTTON_LUA, encoding="utf-8")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(data):
    global _monitor_cache
    _monitor_cache = None          # invalidate on any config change
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

_monitor_cache: dict | None = None

def notify(title, msg):
    """Update the status bar label; safe to call from any thread."""
    _log(f"[notify] {msg}")
    win = _state.get("window")
    if win and win.winfo_exists():
        win.after(0, lambda: _state["status_var"].set(msg))

# ── Image filter ──────────────────────────────────────────────────────────────

def _strip_drawing_lines_arr(img_bgr):
    """Strip TTS drawing lines from an in-memory BGR numpy array.

    Returns (result_bgr, was_filtered).  The input array is not modified.
    If cv2/numpy are unavailable, or no drawing-colour pixels are found,
    the original array is returned unchanged with was_filtered=False.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return img_bgr, False

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB).astype(np.int16)
    h, w    = img_bgr.shape[:2]
    mask    = np.zeros((h, w), dtype=np.uint8)

    for _label, (r, g, b) in DRAWING_COLORS_RGB:
        target = np.array([r, g, b], dtype=np.int16)
        diff   = np.abs(img_rgb - target)
        hit    = np.all(diff <= RGB_TOLERANCE, axis=2)
        mask[hit] = 255

    if int(np.count_nonzero(mask)) == 0:
        return img_bgr, False

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask   = cv2.dilate(mask, kernel, iterations=2)
    result = cv2.inpaint(img_bgr, mask, INPAINT_RADIUS, cv2.INPAINT_TELEA)
    return result, True

# ── Screenshot ────────────────────────────────────────────────────────────────

def take_screenshot(prefetched_monitor: dict | None = None,
                    scores: dict | None = None,
                    cards:  dict | None = None):
    try:
        import mss
        from mss.tools import to_png
    except ImportError:
        notify("TTS Replay", "mss not installed")
        return

    if prefetched_monitor is not None:
        monitor = prefetched_monitor
    else:
        monitor = _get_monitor()

    with _state_lock:
        if _state["manifest"] is None:
            session_stamp = datetime.now().strftime("session_%Y%m%d_%H%M%S")
            session_dir   = STORE_DIR / session_stamp
            session_dir.mkdir(parents=True, exist_ok=True)
            _state["session_dir"] = session_dir
            _state["frame_num"]   = 0
            _state["manifest"]    = {
                "session_id":    session_stamp,
                "session_start": datetime.now().isoformat(),
                "frames":        [],
            }
            with open(session_dir / "manifest.json", "w") as f:
                json.dump(_state["manifest"], f, indent=2)

        session_dir   = _state["session_dir"]
        manifest_path = session_dir / "manifest.json"
        ts            = datetime.now()
        turn          = _state["frame_num"] + 1

    filename = f"turn_{turn:04d}_{ts.strftime('%H%M%S')}.jpg"
    filepath = session_dir / filename

    try:
        import cv2
        import numpy as np
        region = (monitor["left"], monitor["top"],
                  monitor["left"] + monitor["width"],
                  monitor["top"]  + monitor["height"])
        img_bgr = None
        cam = _get_dxcam()
        if cam:
            try:
                frame = cam.grab(region=region)
                if frame is not None:
                    img_bgr = frame.copy()
                else:
                    _log("[dxcam] grab returned None in take_screenshot — falling back to mss")
            except Exception as e:
                _log(f"[dxcam] grab error in take_screenshot, falling back to mss: {e}")
        if img_bgr is None:
            import mss
            with mss.mss() as sct:
                raw = sct.grab(monitor)
                img_bgr = np.frombuffer(raw.bgra, dtype=np.uint8) \
                            .reshape(raw.height, raw.width, 4)[..., :3].copy()
        if FRAME_MAX_WIDTH and img_bgr.shape[1] > FRAME_MAX_WIDTH:
            scale   = FRAME_MAX_WIDTH / img_bgr.shape[1]
            new_w   = FRAME_MAX_WIDTH
            new_h   = round(img_bgr.shape[0] * scale)
            img_bgr = cv2.resize(img_bgr, (new_w, new_h),
                                 interpolation=cv2.INTER_AREA)
        # Strip drawing lines in memory — avoids a second disk read/write.
        img_bgr, stripped = _strip_drawing_lines_arr(img_bgr)
        cv2.imwrite(str(filepath), img_bgr,
                    [cv2.IMWRITE_JPEG_QUALITY, FRAME_JPEG_QUALITY])
    except Exception as e:
        notify("TTS Replay", f"\u26a0 Screenshot failed: {e}")
        return

    with _state_lock:
        entry = {
            "filename":  filename,
            "timestamp": ts.isoformat(),
            "turn":      turn,
        }
        if scores:
            entry["scores"] = scores
        if cards:
            entry["cards"] = cards
        _state["manifest"]["frames"].append(entry)
        with open(manifest_path, "w") as f:
            json.dump(_state["manifest"], f, indent=2)
        _state["frame_num"] += 1

    tag = " (filtered)" if stripped else ""
    notify("TTS Replay", f"Turn {turn} captured{tag}")
    _update_ui()

# ── HTML export ───────────────────────────────────────────────────────────────

def compile_html(session_dir: Path) -> Path | None:
    """Encode all turn_*.jpg frames into a single self-contained HTML replay.

    Slides are streamed frame-by-frame directly to the output file so peak
    memory stays flat regardless of frame count (~5 MB vs ~160 MB for 100 frames
    with the old all-in-memory approach).
    """
    import base64

    frames = sorted(session_dir.glob("turn_*.jpg"))
    if not frames:
        return None

    manifest_path = session_dir / "manifest.json"
    manifest_frames = {}
    if manifest_path.exists():
        with open(manifest_path) as f:
            mdata = json.load(f)
        for entry in mdata.get("frames", []):
            manifest_frames[entry["filename"]] = entry

    # Collect metadata (small — scores/cards/timestamps only, no image data).
    scores_per_frame = []
    cards_per_frame  = []
    timestamps       = []
    for fp in frames:
        entry = manifest_frames.get(fp.name, {})
        scores_per_frame.append(entry.get("scores"))
        cards_per_frame.append(entry.get("cards"))
        timestamps.append(entry.get("timestamp", ""))

    scores_js = json.dumps(scores_per_frame)
    cards_js  = json.dumps(cards_per_frame)
    times_js  = json.dumps(timestamps)
    title     = session_dir.name

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Game Notebook \u2014 {title}</title>
<style>
  :root {{
    --red:    #e05555;
    --blue:   #5588e0;
    --bg:     #0d0f14;
    --panel:  #13161e;
    --card:   #1c2030;
    --border: #252840;
    --text:   #d0d6e8;
    --muted:  #5a6080;
    --accent: #f0c040;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text);
          font-family: "Segoe UI", system-ui, sans-serif;
          display: flex; flex-direction: column; align-items: center;
          min-height: 100vh; padding: 20px 16px; gap: 14px; }}

  /* ── Header ── */
  .header {{ display: flex; align-items: center; justify-content: center;
             gap: 16px; flex-wrap: wrap; }}
  .header-text {{ text-align: center; }}
  .header h1 {{ font-size: 1.3rem; letter-spacing: .08em;
                text-transform: uppercase; color: var(--accent); }}
  .header .sub {{ font-size: .75rem; color: var(--muted); margin-top: 2px; }}
  .btn-save-main {{ background: #2dce6a; color: #000; font-weight: 700;
                    border-color: #2dce6a; padding: 8px 28px; font-size: .9rem; }}

  /* ── Page layout ── */
  .page-layout {{
    display: grid; grid-template-columns: 1fr 70vw 1fr;
    width: 100%; gap: 12px; align-items: stretch;
  }}
  .center-col {{ display: flex; flex-direction: column; gap: 14px; }}

  /* ── Army list side panels ── */
  .army-panel {{ display: flex; flex-direction: column; gap: 6px; padding-top: 4px; }}
  .army-label {{ font-size: .78rem; text-transform: uppercase;
                 letter-spacing: .1em; color: var(--muted); }}
  .army-panel textarea {{
    flex: 1; width: 100%; min-height: 100px; resize: none;
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 6px 8px; font-family: inherit; font-size: .95rem; line-height: 1.4;
  }}
  .army-panel textarea:focus {{ outline: none; border-color: var(--accent); }}

  /* ── Shared cards (above viewer) ── */
  #sharedCardsBar {{ width: 100%; display: none; }}
  #sharedCardsBar.visible {{ display: grid;
    grid-template-columns: 1fr 1fr 1fr; gap: 10px; }}
  .shared-card {{ background: var(--panel); border: 1px solid var(--border);
                  border-radius: 6px; padding: 10px 12px; }}

  /* ── Viewer ── */
  #viewer {{ display: block; }}
  .viewer-wrap {{
    display: flex; flex-direction: column; align-items: center;
    width: 100%; gap: 0;
  }}
  .viewer-img-wrap {{
    position: relative; display: flex;
    width: 100%; height: 80vh;
    justify-content: center; align-items: center;
    background: #000; border-radius: 6px 6px 0 0;
    border: 2px solid var(--border);
    box-shadow: 0 4px 24px #0008;
    overflow: hidden;
  }}
  .viewer-wrap #viewer {{
    width: 100%; height: 100%;
    object-fit: contain;
    display: block; border-radius: 6px 6px 0 0;
  }}
  .controls {{
    display: flex; align-items: center; gap: 8px;
    width: 100%;
    background: var(--panel); border: 2px solid var(--border);
    border-top: none; border-radius: 0;
    padding: 6px 8px;
  }}
  .btn {{ background: var(--card); color: var(--text); border: 1px solid var(--border);
          padding: 6px 14px; cursor: pointer; border-radius: 4px; font-size: .9rem;
          transition: background .15s; white-space: nowrap; flex-shrink: 0; }}
  .btn:hover {{ background: var(--border); }}
  .btn#playBtn {{ min-width: 42px; text-align: center; padding: 6px 10px; }}
  #slider {{ flex: 1; min-width: 0; accent-color: var(--accent); }}
  #label {{ min-width: 80px; text-align: right; font-size: .85rem;
            color: var(--muted); flex-shrink: 0; }}
  #timestamp {{ font-size: .75rem; color: var(--muted); text-align: center; }}

  /* ── Data panel ── */
  #dataPanel {{ width: 100%; display: none; flex-direction: column; gap: 12px; }}
  #dataPanel.visible {{ display: flex; }}
  .players {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .player-block {{ background: var(--panel); border: 1px solid var(--border);
                   border-radius: 8px; padding: 14px; }}
  .player-block.p1 {{ border-top: 3px solid var(--red); }}
  .player-block.p2 {{ border-top: 3px solid var(--blue); }}
  .player-name {{ font-size: 1.13rem; font-weight: 700; margin-bottom: 10px; }}
  .player-block.p1 .player-name {{ color: var(--red); }}
  .player-block.p2 .player-name {{ color: var(--blue); }}
  .player-label {{ font-size: .78rem; text-transform: uppercase;
                   letter-spacing: .1em; color: var(--muted); margin-bottom: 2px; }}
  .card-pill {{ background: var(--card); border: 1px solid var(--border);
                border-radius: 4px; padding: 5px 10px; font-size: .95rem;
                color: var(--text); margin-bottom: 6px; }}
  .card-pill.empty {{ color: var(--muted); font-style: italic; }}
  .player-total {{ font-size: 2.13rem; font-weight: 800; text-align: center;
                   margin-top: 10px; padding-top: 10px;
                   border-top: 1px solid var(--border); }}
  .player-block.p1 .player-total {{ color: var(--red); }}
  .player-block.p2 .player-total {{ color: var(--blue); }}
  .total-label {{ font-size: .83rem; color: var(--muted); text-align: center;
                  text-transform: uppercase; letter-spacing: .08em; }}

    /* ── Round table ── */
  .round-table-wrap {{ background: var(--panel); border: 1px solid var(--border);
                       border-radius: 8px; overflow: hidden; }}
  .round-table-wrap h3 {{ font-size: .83rem; text-transform: uppercase;
                          letter-spacing: .1em; color: var(--muted);
                          padding: 10px 14px 6px; text-align: center; }}
  table.rtable {{ width: 100%; border-collapse: collapse; font-size: .95rem; table-layout: fixed; }}
  /* cols: p1-name | p1-pri | p1-sc1 | p1-sc2 | p1-chl | divider | round-label | divider | p2-chl | p2-sc2 | p2-sc1 | p2-pri | p2-name */
  .rtable col.c-name {{ width: 72px; }}
  .rtable col.c-num  {{ width: 48px; }}
  .rtable col.c-div  {{ width: 8px; }}
  .rtable col.c-rnd  {{ width: 70px; }}
  .rtable th {{ padding: 4px 4px; text-align: center; color: var(--muted);
                font-weight: 600; font-size: .81rem; text-transform: uppercase;
                letter-spacing: .06em; border-bottom: 1px solid var(--border); }}
  .rtable th.c-name-p1 {{ color: var(--red);  font-weight: 800; font-size: .83rem; text-align: left;  padding-left: 8px; }}
  .rtable th.c-name-p2 {{ color: var(--blue); font-weight: 800; font-size: .83rem; text-align: right; padding-right: 8px; }}
  .rtable th.p1h  {{ color: var(--red);  }}
  .rtable th.p2h  {{ color: var(--blue); }}
  .rtable td {{ padding: 5px 4px; text-align: center; border-bottom: 1px solid #1a1d28; }}
  .rtable tr:last-child td {{ border-bottom: none; }}
  .rtable td.c-name-p1 {{ color: var(--muted); font-size: .85rem; text-align: left;  padding-left: 8px; }}
  .rtable td.c-name-p2 {{ color: var(--muted); font-size: .85rem; text-align: right; padding-right: 8px; }}
  .rtable td.c-rnd {{ color: var(--muted); font-size: .85rem; text-align: center; }}
  .rtable td.tot   {{ font-weight: 700; }}
  .rtable td.tot.p1 {{ color: var(--red); }}
  .rtable td.tot.p2 {{ color: var(--blue); }}
  .rtable tr.dim td {{ opacity: .35; }}
  .rtable tr.total-row td {{ border-top: 2px solid var(--border);
                              font-weight: 600; background: #0d0f1a; }}
  .rtable tr.battle-ready td {{ border-bottom: 2px solid var(--border); }}
  .rtable td.c-div, .rtable th.c-div {{ background: var(--border); padding: 0; }}
  .no-data {{ text-align: center; color: var(--muted); font-size: .98rem; padding: 16px; }}

  /* ── Notebook ── */
  .notebook {{ width: 100%; margin-top: 8px; }}
  .notebook h3 {{ font-size: .83rem; text-transform: uppercase; letter-spacing: .1em;
                  color: var(--muted); margin-bottom: 8px; }}
  .notebook-grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }}
  .notebook-cell label {{ display: block; font-size: .78rem; text-transform: uppercase;
                           letter-spacing: .1em; color: var(--muted); margin-bottom: 4px; }}
  .notebook-cell textarea {{
    width: 100%; height: 90px; resize: vertical;
    background: var(--panel); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 6px 8px; font-family: inherit; font-size: .95rem;
    line-height: 1.4;
  }}
  .notebook-cell textarea:focus {{ outline: none; border-color: var(--accent); }}

  /* ── Doodle toolbar ── */
  .doodle-bar {{
    display: flex; align-items: center; gap: 6px;
    width: 100%;
    background: var(--panel); border: 2px solid var(--border);
    border-top: none; border-radius: 0 0 6px 6px;
    padding: 5px 8px;
  }}
  .doodle-bar .btn {{ padding: 4px 12px; font-size: .8rem; }}
  .doodle-bar .btn.active {{ outline: 2px solid var(--accent); }}
  .btn-delete {{ color: var(--red); border-color: #4a2020; margin-left: auto; }}
  .btn-delete:hover {{ background: #3a1a1a; color: #ff6b6b; }}
  .btn-red.active  {{ background: #7a1a1a; outline-color: var(--red); }}
  .btn-blue.active {{ background: #1a2e6e; outline-color: var(--blue); }}
  .btn-draw.active {{ background: #2a3a2a; outline-color: #4caf50; }}
  .doodle-sep {{ width: 1px; height: 20px; background: var(--border); margin: 0 2px; }}

  /* ── Canvas overlay ── */
  #doodleCanvas {{
    position: absolute; top: 0; left: 0;
    width: 100%; height: 100%;
    pointer-events: none;
    border-radius: 6px 6px 0 0;
  }}
  #doodleCanvas.active {{ pointer-events: all; cursor: crosshair; }}
</style>
</head>
<body>
<div class="header">
  <div class="header-text">
    <h1>Fendi's Snapshotbot</h1>
    <div class="sub">{title}</div>
  </div>
  <button class="btn btn-save-main" id="btnSave">&#8681; Save &amp; Download</button>
</div>

<div class="page-layout">
<div class="army-panel">
  <div class="army-label" id="armyLabelLeft">Red Army List</div>
  <textarea id="note_army_red" placeholder="Army list…"></textarea>
</div>
<div class="center-col">
<!-- Shared cards bar — above viewer, populated by JS -->
<div id="sharedCardsBar"></div>

<div class="viewer-wrap">
<div class="viewer-img-wrap">
  <img id="viewer" src="">
  <canvas id="doodleCanvas"></canvas>
</div>
<div class="controls">
  <button class="btn" id="prev">&#8592;</button>
  <button class="btn" id="playBtn">&#9654;</button>
  <button class="btn" id="next">&#8594;</button>
  <input type="range" id="slider" min="0" max="0" value="0">
  <span id="label"></span>
</div>
<div class="doodle-bar" id="doodleBar">
  <button class="btn btn-draw active" id="btnDraw">&#9998; Draw</button>
  <div class="doodle-sep"></div>
  <button class="btn btn-red active"  id="btnRed" >&#9632; Red</button>
  <button class="btn btn-blue"        id="btnBlue">&#9632; Blue</button>
  <div class="doodle-sep"></div>
  <button class="btn" id="btnUndo">&#8630; Undo</button>
  <button class="btn" id="btnClear">&#10005; Clear Frame</button>
  <button class="btn btn-delete" id="btnDelete">&#128465; Delete Frame</button>
</div>
</div>
<div id="timestamp"></div>
<div id="dataPanel"></div>

<div class="notebook">
  <h3>Game Notebook</h3>
  <div class="notebook-grid">
    <div class="notebook-cell"><label>Deployment</label><textarea id="note_deployment" placeholder="Deployment notes…"></textarea></div>
    <div class="notebook-cell"><label>Round 1</label><textarea id="note_round1" placeholder="Round 1 notes…"></textarea></div>
    <div class="notebook-cell"><label>Round 2</label><textarea id="note_round2" placeholder="Round 2 notes…"></textarea></div>
    <div class="notebook-cell"><label>Round 3</label><textarea id="note_round3" placeholder="Round 3 notes…"></textarea></div>
    <div class="notebook-cell"><label>Round 4</label><textarea id="note_round4" placeholder="Round 4 notes…"></textarea></div>
    <div class="notebook-cell"><label>Round 5</label><textarea id="note_round5" placeholder="Round 5 notes…"></textarea></div>
  </div>
</div>
</div><!-- end .center-col -->
<div class="army-panel">
  <div class="army-label" id="armyLabelRight">Blue Army List</div>
  <textarea id="note_army_blue" placeholder="Army list…"></textarea>
</div>
</div><!-- end .page-layout -->

<script id="slidesData"  type="application/json">[__SLIDES__]</script>
<script id="scoresData" type="application/json">{scores_js}</script>
<script id="cardsData"  type="application/json">{cards_js}</script>
<script id="timesData"  type="application/json">{times_js}</script>
<script id="notesData"  type="application/json">__NOTES__</script>
<script>
  const slides = JSON.parse(document.getElementById('slidesData').textContent);
  const scores = JSON.parse(document.getElementById('scoresData').textContent);
  const cards  = JSON.parse(document.getElementById('cardsData').textContent);
  const times  = JSON.parse(document.getElementById('timesData').textContent);

  const img        = document.getElementById('viewer');
  const slider     = document.getElementById('slider');
  const labelEl    = document.getElementById('label');
  const tsEl       = document.getElementById('timestamp');
  const panel      = document.getElementById('dataPanel');
  const sharedBar  = document.getElementById('sharedCardsBar');
  const playBtn    = document.getElementById('playBtn');
  let cur      = 0;
  let playing  = false;
  let playTimer = null;
  const PLAY_INTERVAL_MS = 1000;

  slider.max = slides.length - 1;

  /* ── Doodle system ── */
  const canvas    = document.getElementById('doodleCanvas');
  const ctx       = canvas.getContext('2d');
  const doodleBar = document.getElementById('doodleBar');
  const btnDraw   = document.getElementById('btnDraw');
  const btnRed    = document.getElementById('btnRed');
  const btnBlue   = document.getElementById('btnBlue');
  const btnUndo   = document.getElementById('btnUndo');
  const btnClear  = document.getElementById('btnClear');
  const btnDelete = document.getElementById('btnDelete');
  const btnSave   = document.getElementById('btnSave');

  // strokes[frameIndex] = [ {{color, points:[{{x,y}},...]}}, ... ]
  // Per-frame — each frame has its own independent doodles.
  const strokes = slides.map(() => []);
  let drawMode    = true;   // on by default since bar is always visible
  let activeColor = '#ff1a1a';
  let isDrawing   = false;
  let currentStroke = null;

  function syncCanvasSize() {{
    const r = img.getBoundingClientRect();
    canvas.width  = r.width;
    canvas.height = r.height;
    redrawCanvas();
  }}

  function redrawCanvas() {{
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const scaleX = canvas.width  / img.naturalWidth;
    const scaleY = canvas.height / img.naturalHeight;
    for (const stroke of strokes[cur]) {{
      if (stroke.points.length < 2) continue;
      ctx.beginPath();
      ctx.strokeStyle = stroke.color;
      ctx.lineWidth   = 3;
      ctx.lineJoin    = 'round';
      ctx.lineCap     = 'round';
      ctx.moveTo(stroke.points[0].x * scaleX, stroke.points[0].y * scaleY);
      for (let i = 1; i < stroke.points.length; i++) {{
        ctx.lineTo(stroke.points[i].x * scaleX, stroke.points[i].y * scaleY);
      }}
      ctx.stroke();
    }}
  }}

  function canvasPos(e) {{
    const r = canvas.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const clientY = e.touches ? e.touches[0].clientY : e.clientY;
    const scaleX = img.naturalWidth  / r.width;
    const scaleY = img.naturalHeight / r.height;
    return {{ x: (clientX - r.left) * scaleX, y: (clientY - r.top) * scaleY }};
  }}

  function setDrawMode(on) {{
    drawMode = on;
    btnDraw.classList.toggle('active', on);
    canvas.classList.toggle('active', on);
    if (on && playing) setPlaying(false);
  }}

  canvas.addEventListener('mousedown',  e => {{
    if (!drawMode) return;
    isDrawing     = true;
    currentStroke = {{ color: activeColor, points: [canvasPos(e)] }};
  }});
  canvas.addEventListener('mousemove',  e => {{
    if (!isDrawing) return;
    currentStroke.points.push(canvasPos(e));
    redrawCanvas();
    const scaleX = canvas.width  / img.naturalWidth;
    const scaleY = canvas.height / img.naturalHeight;
    const pts = currentStroke.points;
    ctx.beginPath();
    ctx.strokeStyle = currentStroke.color;
    ctx.lineWidth   = 3; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.moveTo(pts[0].x * scaleX, pts[0].y * scaleY);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x * scaleX, pts[i].y * scaleY);
    ctx.stroke();
  }});
  const finishStroke = () => {{
    if (isDrawing && currentStroke && currentStroke.points.length > 1)
      strokes[cur].push(currentStroke);
    isDrawing = false; currentStroke = null;
  }};
  canvas.addEventListener('mouseup',    finishStroke);
  canvas.addEventListener('mouseleave', finishStroke);

  btnDraw.onclick  = () => setDrawMode(!drawMode);
  btnRed.onclick   = () => {{ activeColor = '#ff1a1a'; btnRed.classList.add('active');  btnBlue.classList.remove('active'); }};
  btnBlue.onclick  = () => {{ activeColor = '#1a8fff'; btnBlue.classList.add('active'); btnRed.classList.remove('active'); }};
  btnUndo.onclick  = () => {{ strokes[cur].pop(); redrawCanvas(); }};
  btnClear.onclick = () => {{ strokes[cur] = []; redrawCanvas(); }};

  btnDelete.onclick = () => {{
    if (slides.length <= 1) {{ alert('Cannot delete the only frame.'); return; }}
    const _msg = 'Delete this frame?' + String.fromCharCode(10,10) + 'This will be permanent once you Save & Download.';
    if (!confirm(_msg)) return;
    slides.splice(cur, 1);
    scores.splice(cur, 1);
    cards.splice(cur, 1);
    times.splice(cur, 1);
    strokes.splice(cur, 1);
    // Update small data islands immediately (cheap — no image data).
    document.getElementById('scoresData').textContent = JSON.stringify(scores);
    document.getElementById('cardsData').textContent  = JSON.stringify(cards);
    document.getElementById('timesData').textContent  = JSON.stringify(times);
    // slidesData is large — deferred to save time to avoid blocking the UI.
    slider.max = Math.max(0, slides.length - 1);
    show(Math.min(cur, slides.length - 1));
  }};

  btnSave.onclick = () => {{
    const notes = {{}};
    NOTEBOOK_KEYS.forEach(k => {{
      const ta = document.getElementById('note_' + k);
      notes[k] = ta ? ta.value : '';
    }});
    let html = document.documentElement.outerHTML;
    html = html.replace('const strokes = slides.map(() => []);',
                        'const strokes = ' + JSON.stringify(strokes) + ';');
    const notesTag  = '<scr' + 'ipt id="notesData" type="application/json">';
    const slidesTag = '<scr' + 'ipt id="slidesData" type="application/json">';
    const closeTag  = '<\/scr' + 'ipt>';
    html = html.replace(
        new RegExp(notesTag  + '[\\s\\S]*?' + closeTag),
        notesTag  + JSON.stringify(notes)  + closeTag);
    html = html.replace(
        new RegExp(slidesTag + '[\\s\\S]*?' + closeTag),
        slidesTag + JSON.stringify(slides) + closeTag);
    const blob = new Blob([html], {{type: 'text/html'}});
    const a    = document.createElement('a');
    a.href     = URL.createObjectURL(blob);
    a.download = location.pathname.split('/').pop() || 'replay.html';
    a.click();
    URL.revokeObjectURL(a.href);
  }};

  // Start with draw mode on
  setDrawMode(true);
  new ResizeObserver(syncCanvasSize).observe(img);
  img.addEventListener('load', syncCanvasSize);

  /* ── Playback ── */
  function setPlaying(on) {{
    playing = on;
    playBtn.innerHTML = playing ? '&#9646;&#9646;' : '&#9654;';
    if (playing) {{
      playTimer = setInterval(() => {{
        if (cur >= slides.length - 1) {{
          show(0);           // loop back
        }} else {{
          show(cur + 1);
        }}
      }}, PLAY_INTERVAL_MS);
    }} else {{
      clearInterval(playTimer);
      playTimer = null;
    }}
  }}

  playBtn.onclick = () => setPlaying(!playing);

  /* ── Helpers ── */
  function card(name) {{
    if (!name) return '<div class="card-pill empty">\u2014 none \u2014</div>';
    return `<div class="card-pill">${{name}}</div>`;
  }}

  function renderSharedCards(c) {{
    if (!c) {{ sharedBar.className = ''; sharedBar.innerHTML = ''; return; }}
    sharedBar.className = 'visible';
    sharedBar.innerHTML = `
      <div class="shared-card"><div class="player-label">Deployment</div>${{card(c.deployment)}}</div>
      <div class="shared-card"><div class="player-label">Primary Mission</div>${{card(c.primary)}}</div>
      <div class="shared-card"><div class="player-label">Challenger Card</div>${{card(c.challenger)}}</div>`;
  }}

  function renderPanel(s, c) {{
    if (!s && !c) {{
      panel.className = 'visible';
      panel.innerHTML = '<div class="no-data">No score or card data for this turn</div>';
      return;
    }}
    const p1 = s ? s.red  : null;
    const p2 = s ? s.blue : null;
    const p1name = p1 ? p1.name : 'Player 1 (Red)';
    const p2name = p2 ? p2.name : 'Player 2 (Blue)';

    const p1Block = `
      <div class="player-block p1">
        <div class="player-name">\u25a0 ${{p1name}}</div>
        <div class="player-label">Secondary 1</div>${{card(c && c.p1_sec1)}}
        <div class="player-label">Secondary 2</div>${{card(c && c.p1_sec2)}}
        ${{p1 ? `<div class="player-total">${{p1.total}}</div>
          <div class="total-label">Total VP</div>` : ''}}
      </div>`;

    const p2Block = `
      <div class="player-block p2">
        <div class="player-name">\u25a0 ${{p2name}}</div>
        <div class="player-label">Secondary 1</div>${{card(c && c.p2_sec1)}}
        <div class="player-label">Secondary 2</div>${{card(c && c.p2_sec2)}}
        ${{p2 ? `<div class="player-total">${{p2.total}}</div>
          <div class="total-label">Total VP</div>` : ''}}
      </div>`;

    /* Round-by-round table: p1-name | PRI SC1 SC2 CHL | round | CHL SC2 SC1 PRI | p2-name */
    let roundRows = '';
    if (s) {{
      const rounds = Math.max(p1.rounds.length, p2.rounds.length);

      // Battle Ready row (painted bonus — always 10 each)
      roundRows += `<tr class="battle-ready">
        <td class="c-name-p1">Battle Ready</td>
        <td class="tot p1">10</td><td>—</td><td>—</td><td>—</td><td>—</td>
        <td class="c-div"></td>
        <td class="c-rnd">Battle Ready</td>
        <td class="c-div"></td>
        <td>—</td><td>—</td><td>—</td><td>—</td><td class="tot p2">10</td>
        <td class="c-name-p2">Battle Ready</td>
      </tr>`;

      for (let r = 0; r < rounds; r++) {{
        const r1 = p1.rounds[r] || {{}}, r2 = p2.rounds[r] || {{}};
        const hasScore = (r1.total || 0) + (r2.total || 0) > 0;
        roundRows += `<tr class="${{hasScore ? '' : 'dim'}}">
          <td class="c-name-p1">Round ${{r + 1}}</td>
          <td class="tot p1">${{r1.total      ?? '-'}}</td>
          <td>${{r1.primary    ?? '-'}}</td>
          <td>${{r1.sec1       ?? '-'}}</td>
          <td>${{r1.sec2       ?? '-'}}</td>
          <td>${{r1.challenger ?? '-'}}</td>
          <td class="c-div"></td>
          <td class="c-rnd">Round ${{r + 1}}</td>
          <td class="c-div"></td>
          <td>${{r2.challenger ?? '-'}}</td>
          <td>${{r2.sec2       ?? '-'}}</td>
          <td>${{r2.sec1       ?? '-'}}</td>
          <td>${{r2.primary    ?? '-'}}</td>
          <td class="tot p2">${{r2.total      ?? '-'}}</td>
          <td class="c-name-p2">Round ${{r + 1}}</td>
        </tr>`;
      }}
      roundRows += `<tr class="total-row">
        <td class="c-name-p1">Totals</td>
        <td class="tot p1">${{p1.total}}</td>
        <td>${{p1.primary}}</td>
        <td colspan="2">${{p1.secondary}}</td>
        <td>${{p1.challenger}}</td>
        <td class="c-div"></td>
        <td class="c-rnd">Totals</td>
        <td class="c-div"></td>
        <td>${{p2.challenger}}</td>
        <td colspan="2">${{p2.secondary}}</td>
        <td>${{p2.primary}}</td>
        <td class="tot p2">${{p2.total}}</td>
        <td class="c-name-p2">Totals</td>
      </tr>`;
    }}

    const roundTable = s ? `
      <div class="round-table-wrap">
        <h3>Round by Round</h3>
        <table class="rtable">
          <colgroup>
            <col class="c-name">
            <col class="c-num"><col class="c-num"><col class="c-num"><col class="c-num"><col class="c-num">
            <col class="c-div">
            <col class="c-rnd">
            <col class="c-div">
            <col class="c-num"><col class="c-num"><col class="c-num"><col class="c-num"><col class="c-num">
            <col class="c-name">
          </colgroup>
          <thead><tr>
            <th class="c-name-p1">\u25a0 ${{p1name}}</th>
            <th class="p1h">TOT</th>
            <th class="p1h">PRI</th>
            <th class="p1h">SC1</th>
            <th class="p1h">SC2</th>
            <th class="p1h">CHL</th>
            <th class="c-div"></th>
            <th style="color:var(--muted);font-size:.65rem"></th>
            <th class="c-div"></th>
            <th class="p2h">CHL</th>
            <th class="p2h">SC2</th>
            <th class="p2h">SC1</th>
            <th class="p2h">PRI</th>
            <th class="p2h">TOT</th>
            <th class="c-name-p2">${{p2name}} \u25a0</th>
          </tr></thead>
          <tbody>${{roundRows}}</tbody>
        </table>
      </div>` : '';

    panel.className = 'visible';
    panel.innerHTML = `<div class="players">${{p1Block}}${{p2Block}}</div>${{roundTable}}`;
  }}

  function fmt(iso) {{
    if (!iso) return '';
    try {{ return new Date(iso).toLocaleTimeString([], {{hour:'2-digit',minute:'2-digit'}}); }}
    catch(e) {{ return iso; }}
  }}

  function show(n) {{
    cur = Math.max(0, Math.min(slides.length - 1, n));
    img.src             = slides[cur];
    slider.value        = cur;
    labelEl.textContent = `Frame ${{cur + 1}} / ${{slides.length}}`;
    tsEl.textContent    = times[cur] ? `Captured ${{fmt(times[cur])}}` : '';
    renderSharedCards(cards[cur]);
    renderPanel(scores[cur], cards[cur]);
    redrawCanvas();
  }}

  document.getElementById('prev').onclick = () => {{ setPlaying(false); show(cur - 1); }};
  document.getElementById('next').onclick = () => {{ setPlaying(false); show(cur + 1); }};
  slider.oninput = () => {{ setPlaying(false); show(+slider.value); }};
  document.addEventListener('keydown', e => {{
    if (document.activeElement && (document.activeElement.tagName === 'TEXTAREA' || document.activeElement.tagName === 'INPUT')) return;
    if (e.key === 'ArrowLeft')  {{ setPlaying(false); show(cur - 1); }}
    if (e.key === 'ArrowRight') {{ setPlaying(false); show(cur + 1); }}
    if (e.key === ' ')          {{ e.preventDefault(); setPlaying(!playing); }}
  }});
  show(0);

  /* ── Notebook ── */
  const NOTEBOOK_KEYS = ['deployment','round1','round2','round3','round4','round5','army_red','army_blue'];

  // Load saved notes if embedded, otherwise empty
  const savedNotes = JSON.parse(document.getElementById('notesData').textContent);
  NOTEBOOK_KEYS.forEach(k => {{
    const ta = document.getElementById('note_' + k);
    if (ta && savedNotes[k]) ta.value = savedNotes[k];
  }});

  // Label army panels with actual player names
  const firstScores = scores.find(s => s);
  if (firstScores) {{
    document.getElementById('armyLabelLeft').textContent  = (firstScores.red?.name  || 'Red')  + ' Army List';
    document.getElementById('armyLabelRight').textContent = (firstScores.blue?.name || 'Blue') + ' Army List';
  }}
</script>
</body>
</html>"""

    # Split the template at the slides sentinel so we can stream image data
    # frame-by-frame without ever holding all base64 strings in memory at once.
    # __NOTES__ is also resolved here (it lives in the pre-sentinel portion).
    pre, post = html.split("[__SLIDES__]", 1)
    post = post.replace("__NOTES__", "{}")

    out      = STORE_DIR / f"Game Notebook {session_dir.name}.html"
    n_frames = len(frames)
    with open(out, "w", encoding="utf-8", buffering=1 << 20) as fh:
        fh.write(pre)
        fh.write("[")
        for i, fp in enumerate(frames):
            if i:
                fh.write(",\n")
            data = base64.b64encode(fp.read_bytes()).decode()
            fh.write('"data:image/jpeg;base64,')
            fh.write(data)
            fh.write('"')
            del data          # release immediately; don't accumulate
            if i % 5 == 0 or i == n_frames - 1:
                notify("TTS Replay", f"Compiling replay\u2026 ({i + 1}/{n_frames})")
        fh.write("]")
        fh.write(post)

    return out

# ── TTS TCP listener ───────────────────────────────────────────────────────────
# TTS owns port 39998 as the server. We connect as a client and keep the
# connection open. sendExternalMessage() in Lua pushes JSON to us; we can
# also send JSON back and TTS receives it via onExternalMessage().

# ── TTS communication ─────────────────────────────────────────────────────────
# Per TTS External Editor API (https://api.tabletopsimulator.com/externaleditorapi/):
#
#   Port 39998 — Python listens as SERVER.
#                TTS connects here and sends messageID 4 for each
#                sendExternalMessage() call. Each call is a new short connection.
#
#   Port 39999 — TTS listens as SERVER.
#                Python connects here to send messageID 2 (Custom Message),
#                which TTS delivers to onExternalMessage() in the Lua script.
#                messageID 3 = Execute Lua (requires guid + object must have script).
#
# Flow:
#   1. Python starts session → _listener_thread binds port 39998
#   2. Python sends messageID 2 {"action":"handshake"} → TTS port 39999
#   3. TTS onExternalMessage receives it → sets connected = true
#   4. User presses CAPTURE → runSequence → sendExternalMessage → messageID 4
#      arrives at Python port 39998 → _handle_connection → take screenshot

def _send_to_tts(custom_data: dict):
    """Send a custom message to TTS onExternalMessage via port 39999 (messageID 2)."""
    payload = {"messageID": 2, "customMessage": custom_data}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("localhost", TTS_SEND_PORT))
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        s.close()
    except ConnectionRefusedError:
        notify("TTS Replay", "\u26a0 TTS not reachable on port 39999 \u2014 is Tabletop Simulator running?")
    except socket.timeout:
        notify("TTS Replay", "\u26a0 TTS send timed out")
    except Exception as e:
        notify("TTS Replay", f"\u26a0 TTS send failed: {e}")

def _dispatch_action(action: str,
                     scores: dict | None = None,
                     cards:  dict | None = None):
    if action == "capture":
        threading.Thread(target=_delayed_capture,
                         kwargs={"scores": scores, "cards": cards},
                         daemon=True).start()
    elif action == "capture_auto":
        threading.Thread(target=_delayed_capture,
                         kwargs={"skip_on_unstable": True,
                                 "scores": scores, "cards": cards},
                         daemon=True).start()

def _handle_connection(conn):
    """Read one complete JSON message from a short-lived TTS connection.

    TTS closes the socket after sending — no newline terminator is guaranteed.
    We read until recv() returns empty (EOF), then parse the accumulated buffer.
    The buffer may contain multiple JSON objects if TTS batches them; we try
    each line as well as the whole buffer.
    """
    buf = b""
    try:
        conn.settimeout(2.0)
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk

        text = buf.decode("utf-8", errors="ignore").strip()
        if not text:
            return

        # Try to parse: first as a whole blob, then line-by-line
        candidates = []
        try:
            candidates.append(json.loads(text))
        except json.JSONDecodeError:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        for msg in candidates:
            if not isinstance(msg, dict):
                continue
            custom = msg.get("customMessage") or {}
            action = custom.get("action")
            scores = custom.get("scores")
            cards  = custom.get("cards")
            if action in ("capture", "capture_auto"):
                notify("TTS Replay", "Signal received — capturing…")
                _dispatch_action(action, scores, cards)
            # Silently ignore non-capture actions (handshake echoes, etc.)

    except Exception as e:
        notify("TTS Replay", f"\u26a0 Connection read error: {e}")
    finally:
        conn.close()

def _listener_thread():
    """HTTP server on port 39997. Lua WebRequest.post() signals captures here."""
    import http.server
    from urllib.parse import unquote_plus

    notify("TTS Replay", "Waiting for TTS…")
    _log(f"[listener] starting on 127.0.0.1:{TTS_LISTEN_PORT}")
    _send_to_tts({"action": "handshake"})

    def _poll_loop():
        while _state["listening"]:
            time.sleep(5)
            if _state["listening"]:
                _send_to_tts({"action": "poll"})
    threading.Thread(target=_poll_loop, daemon=True).start()

    dispatch = _dispatch_action

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            _log(f"[http] {fmt % args}")
        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw    = self.rfile.read(length).decode("utf-8", errors="ignore")
                body   = unquote_plus(raw).strip()
                _log(f"[http] POST {self.path} body={body[:400]}")
                data   = json.loads(body) if body else {}
                action = data.get("action", "")
                if action in ("capture", "capture_auto"):
                    scores = data.get("scores")
                    cards  = data.get("cards")
                    notify("TTS Replay", "Signal received — capturing…")
                    dispatch(action, scores, cards)
                elif action == "refresh":
                    # Echo back so onExternalMessage can refresh the cache
                    # and advance the pipeline to phase 2 in a safe context.
                    threading.Thread(
                        target=_send_to_tts,
                        args=({"action": "refresh_done"},),
                        daemon=True
                    ).start()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                import traceback
                _log(f"[http] error: {e}\n{traceback.format_exc()}")
                self.send_response(500)
                self.end_headers()
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"TTS Replay running")

    try:
        httpd = http.server.HTTPServer(("127.0.0.1", TTS_LISTEN_PORT), _Handler)
        httpd.timeout = 1.0
        _log(f"[listener] bound on 127.0.0.1:{TTS_LISTEN_PORT}")
        while _state["listening"]:
            httpd.handle_request()
        httpd.server_close()
    except OSError as e:
        notify("TTS Replay", f"Cannot bind port {TTS_LISTEN_PORT}: {e}")
        _log(f"[listener] bind failed: {e}")


def _grab_frame_mss(sct, monitor: dict):
    """Grab via mss (BitBlt). Works on windowed/borderless but not HAGS fullscreen."""
    import numpy as np
    raw = sct.grab(monitor)
    return np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)[..., :3]


# dxcam camera instance — created once, reused across captures.
_dxcam_instance = None
_dxcam_lock     = threading.Lock()

def _get_dxcam():
    """Return a shared dxcam.DXCamera, or None if dxcam is unavailable."""
    global _dxcam_instance
    with _dxcam_lock:
        if _dxcam_instance is not None:
            return _dxcam_instance
        try:
            import dxcam
            _dxcam_instance = dxcam.create(output_color="BGR")
            _log("[dxcam] initialised — using Desktop Duplication API")
        except Exception as e:
            _log(f"[dxcam] unavailable, falling back to mss: {e}")
            _dxcam_instance = False   # sentinel: don't retry
        return _dxcam_instance


def _grab_frame(sct, monitor: dict):
    """Grab a screen region and return it as a numpy uint8 array (H×W×3 BGR).

    Tries dxcam first (Desktop Duplication API — works with HAGS and GPU-composed
    fullscreen frames that BitBlt/mss cannot capture).  Falls back to mss if
    dxcam is unavailable or returns None.
    Caller owns the mss context so it can be reused across the stability loop.
    """
    import numpy as np
    region = (monitor["left"], monitor["top"],
              monitor["left"] + monitor["width"],
              monitor["top"]  + monitor["height"])
    cam = _get_dxcam()
    if cam:
        try:
            frame = cam.grab(region=region)
            if frame is not None:
                return frame   # already BGR H×W×3
            _log("[dxcam] grab returned None — falling back to mss for this frame")
        except Exception as e:
            _log(f"[dxcam] grab error, falling back to mss: {e}")
    return _grab_frame_mss(sct, monitor)


def _frames_stable(a, b) -> bool:
    """Compare two numpy frames (H×W×3).  Returns True when ≥ STABILITY_THRESHOLD
    of pixels are identical — uses vectorised numpy ops, not a Python pixel loop.
    """
    import numpy as np
    if a.shape != b.shape:
        return False
    same = int(np.count_nonzero(np.all(a == b, axis=2)))
    total = a.shape[0] * a.shape[1]
    return (same / total) >= STABILITY_THRESHOLD

def _find_tts_hwnd() -> int | None:
    """Return the HWND of the Tabletop Simulator window, or None.

    Uses c_void_p for the HWND so it is safe on 64-bit Windows where a plain
    c_int silently truncates the handle and the window is never found.
    The WNDENUMPROC signature must also use c_void_p for both parameters to
    match the actual Win32 callback prototype (HWND, LPARAM are pointer-sized).
    """
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        # HWND and LPARAM are both pointer-sized — use c_void_p, not c_int.
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        found = ctypes.c_void_p(0)

        def _enum(hwnd, _):
            if ctypes.windll.user32.IsWindowVisible(hwnd):
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                if "tabletop simulator" in buf.value.lower():
                    found.value = hwnd
            return True

        ctypes.windll.user32.EnumWindows(WNDENUMPROC(_enum), 0)
        return found.value  # None if never set, int HWND otherwise
    except Exception as e:
        _log(f"[hwnd] EnumWindows failed: {e}")
        return None


def _get_monitor() -> dict:
    """Return the configured region, or the monitor TTS is on.

    Result is cached in _monitor_cache after the first call and reused until
    save_config() is called (e.g. after recalibration).
    """
    global _monitor_cache
    if _monitor_cache is not None:
        return _monitor_cache

    import mss
    cfg    = load_config()
    region = cfg.get("region")
    if region and region.get("width", 0) >= 10 and region.get("height", 0) >= 10:
        _monitor_cache = {k: region[k] for k in ("left", "top", "width", "height")}
        return _monitor_cache

    tts_monitor = None
    hwnd = _find_tts_hwnd()
    if hwnd:
        try:
            import ctypes, ctypes.wintypes
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            wx, wy = rect.left, rect.top
            with mss.mss() as sct:
                for m in sct.monitors[1:]:
                    if m["left"] <= wx < m["left"] + m["width"] and \
                       m["top"]  <= wy < m["top"]  + m["height"]:
                        tts_monitor = m
                        _log(f"[monitor] TTS found on monitor left={m['left']} top={m['top']}")
                        break
            if not tts_monitor:
                _log(f"[monitor] TTS window at ({wx},{wy}) didn't match any monitor — using monitor 1")
        except Exception as e:
            _log(f"[monitor] rect lookup failed: {e}")
    else:
        _log("[monitor] TTS window not found via EnumWindows — using monitor 1")

    with mss.mss() as sct:
        m = tts_monitor or sct.monitors[1]
        _monitor_cache = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}
    return _monitor_cache

def _delayed_capture(skip_on_unstable: bool = False,
                     scores: dict | None = None,
                     cards:  dict | None = None):
    try:
        import mss
        monitor = _get_monitor()

        if CAMERA_SETTLE_MS > 0:
            time.sleep(CAMERA_SETTLE_MS / 1000)

        # One mss context for all stability grabs — avoids repeated init overhead.
        with mss.mss() as sct:
            prev  = _grab_frame(sct, monitor)
            polls = 0
            while polls < STABILITY_MAX_POLLS:
                time.sleep(STABILITY_POLL_MS / 1000)
                curr = _grab_frame(sct, monitor)
                if _frames_stable(prev, curr):
                    break
                prev  = curr
                polls += 1

        if polls == STABILITY_MAX_POLLS:
            if skip_on_unstable:
                notify("TTS Replay", "\u26a0 Auto-capture skipped \u2014 camera still moving")
                return
            notify("TTS Replay", "\u26a0 Camera may not have settled \u2014 frame captured anyway")

        # Bring TTS to the foreground so mss doesn't grab a black/occluded frame.
        try:
            import ctypes
            hwnd = _find_tts_hwnd()
            if hwnd:
                # SW_RESTORE (9) un-minimises if needed; then SetForegroundWindow
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.15)  # let the window paint before grabbing
        except Exception as fg_err:
            _log(f"[capture] foreground-focus failed (non-fatal): {fg_err}")

        take_screenshot(prefetched_monitor=monitor, scores=scores, cards=cards)
        _send_to_tts({"action": "capture_done"})
    except Exception as e:
        import traceback
        notify("TTS Replay", f"\u26a0 Capture error: {e}  {traceback.format_exc()[-300:]}")

# ── Session control ───────────────────────────────────────────────────────────

def start_listening():
    if _state["listening"]:
        return
    _state["listening"]   = True
    _state["manifest"]    = None
    _state["session_dir"] = None
    _state["frame_num"]   = 0
    threading.Thread(target=_listener_thread, daemon=True).start()
    _update_ui()

def stop_listening():
    if not _state["listening"]:
        return
    _state["listening"] = False
    _update_ui()

    session_dir = _state.get("session_dir")
    if session_dir and _state.get("frame_num", 0) > 0:
        def _compile():
            try:
                notify("TTS Replay", "Compiling replay\u2026")
                html_path = compile_html(session_dir)
                if not html_path:
                    notify("TTS Replay", "\u26a0 Compile failed \u2014 no frames found")
                    return
                # Verify output is non-empty before deleting source frames.
                if html_path.stat().st_size == 0:
                    _log("[compile] output file is zero bytes \u2014 source frames preserved")
                    notify("TTS Replay", "\u26a0 Compile failed \u2014 output file is empty")
                    return
                import shutil
                shutil.rmtree(session_dir, ignore_errors=True)
                notify("TTS Replay", f"Replay saved: {html_path.name}")
                import webbrowser
                try:
                    webbrowser.open(html_path.as_uri())
                except Exception as wb_err:
                    _log(f"[compile] webbrowser.open failed: {wb_err}")
                    notify("TTS Replay", f"Saved \u2014 open manually: {html_path}")
            except Exception as e:
                import traceback
                _log(f"[compile] error: {traceback.format_exc()}")
                notify("TTS Replay", f"\u26a0 Compile error: {e}")
        # Non-daemon so the thread finishes even if the window is closed.
        threading.Thread(target=_compile, daemon=False).start()
    else:
        notify("TTS Replay", "Session ended \u2014 no frames captured")

# ── Calibration ───────────────────────────────────────────────────────────────

def _run_calibrate():
    import tkinter as tk

    result = {}

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)
    root.configure(bg="black", cursor="crosshair")
    root.title("Drag to select battlefield region \u2014 Esc to cancel")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    s = {"start": None, "rect": None}

    def on_press(e):
        s["start"] = (e.x, e.y)
        if s["rect"]: canvas.delete(s["rect"])

    def on_drag(e):
        if s["start"]:
            x0, y0 = s["start"]
            if s["rect"]: canvas.delete(s["rect"])
            s["rect"] = canvas.create_rectangle(
                x0, y0, e.x, e.y,
                outline="#00ff88", width=3,
                fill="#00ff88", stipple="gray25"
            )

    def on_release(e):
        if s["start"] is None:
            return
        x0, y0 = s["start"]
        x1, y1 = e.x, e.y
        if abs(x1 - x0) < 10 or abs(y1 - y0) < 10:
            return
        result["region"] = {
            "left":   min(x0, x1), "top":    min(y0, y1),
            "width":  abs(x1 - x0), "height": abs(y1 - y0),
        }
        root.destroy()

    def on_escape(e): root.destroy()

    canvas.bind("<ButtonPress-1>",   on_press)
    canvas.bind("<B1-Motion>",       on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>",            on_escape)
    root.mainloop()

    if "region" in result:
        cfg = load_config()
        cfg["region"] = result["region"]
        save_config(cfg)
        _update_ui()
        notify("TTS Replay", "Region saved")
    else:
        notify("TTS Replay", "Calibration cancelled")

def do_calibrate():
    COUNTDOWN = 3

    def _countdown_then_calibrate():
        for i in range(COUNTDOWN, 0, -1):
            notify("TTS Replay", f"Switch to TTS\u2026 calibrating in {i}s  (Esc to cancel)")
            time.sleep(1)
        notify("TTS Replay", "Draw the battlefield region\u2026")
        _run_calibrate()

    threading.Thread(target=_countdown_then_calibrate, daemon=True).start()

# ── Taskbar window ────────────────────────────────────────────────────────────

def _update_ui():
    win = _state.get("window")
    if not win or not win.winfo_exists():
        return
    win.after(0, _apply_ui_state)

def _apply_ui_state():
    listening  = _state["listening"]
    has_region = "region" in load_config()
    frame_num  = _state.get("frame_num", 0)

    if listening:
        _state["status_var"].set(
            f"\u25cf RECORDING  \u2014  {frame_num} frame{'s' if frame_num != 1 else ''} captured"
        )
        _state["indicator"].config(bg="#2dce6a")
        _state["btn_session"].config(
            text="\u25a0  Stop Session", bg="#c8391a",
            command=stop_listening
        )
    else:
        _state["indicator"].config(bg="#c8391a")
        _state["btn_session"].config(
            text="\u25b6  Start Session", bg="#2dce6a",
            command=start_listening,
            state="normal"
        )
        if frame_num == 0:
            if not has_region:
                _state["status_var"].set("Ready \u2014 no region set, will capture full screen")
            else:
                _state["status_var"].set("Ready \u2014 start a session to begin recording")

def _exit_app():
    from tkinter import messagebox
    if _state["listening"] and _state.get("frame_num", 0) > 0:
        answer = messagebox.askyesnocancel(
            "Fendi's Snapshotbot \u2014 Exit?",
            f"You have {_state['frame_num']} captured frame(s) in an active session.\n\n"
            "Do you want to compile them into a replay before exiting?\n\n"
            "  Yes   \u2192 compile replay, then exit\n"
            "  No    \u2192 exit without saving\n"
            "  Cancel \u2192 go back",
            icon="warning",
        )
        if answer is True:
            stop_listening()
            time.sleep(3)
            _state["window"].destroy()
        elif answer is False:
            _state["listening"] = False
            _state["window"].destroy()
    else:
        _state["listening"] = False
        _state["window"].destroy()

def _build_window():
    import tkinter as tk

    win = tk.Tk()
    win.title(f"Fendi's Snapshotbot v{VERSION}")
    win.configure(bg="#1a1e26")
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", _exit_app)

    _state["window"]     = win
    _state["status_var"] = tk.StringVar(value="Initialising\u2026")

    top = tk.Frame(win, bg="#1a1e26")
    top.pack(fill="x", padx=12, pady=(12, 4))

    indicator = tk.Label(top, width=2, bg="#c8391a", relief="flat")
    indicator.pack(side="left", fill="y", padx=(0, 8))
    _state["indicator"] = indicator

    tk.Label(top, textvariable=_state["status_var"],
             bg="#1a1e26", fg="#c8d4e0",
             font=("Segoe UI", 9), anchor="w",
             wraplength=300, justify="left").pack(side="left", fill="x", expand=True)

    btn_frame = tk.Frame(win, bg="#1a1e26")
    btn_frame.pack(fill="x", padx=12, pady=(4, 12))

    btn_opts = dict(font=("Segoe UI", 10, "bold"), relief="flat",
                    padx=10, pady=6, cursor="hand2", fg="#ffffff", width=22)

    btn_session = tk.Button(btn_frame, text="\u25b6  Start Session",
                            bg="#2dce6a", **btn_opts)
    btn_session.pack(fill="x", pady=(0, 4))
    _state["btn_session"] = btn_session

    tk.Button(btn_frame, text="\U0001f3af  Calibrate Region",
              bg="#2a3040", command=do_calibrate, **btn_opts).pack(fill="x", pady=(0, 4))

    tk.Button(btn_frame, text="Exit",
              bg="#111418", command=_exit_app, **btn_opts).pack(fill="x")

    _apply_ui_state()
    return win

# ── Single-instance guard ─────────────────────────────────────────────────────

def _check_single_instance():
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "FendisSnapshotbot_SingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Already Running",
            "Fendi's Snapshotbot is already running.\n\nCheck your taskbar.",
        )
        root.destroy()
        sys.exit(0)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _log(f"[startup] version {VERSION}")
    _check_single_instance()
    ensure_assets()
    win = _build_window()
    win.mainloop()
