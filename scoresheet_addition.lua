-- ── Add this function to the GT Scoresheet Lua script ────────────────────────
-- It reads the zone references the scoresheet already holds, so TTS won't
-- block it with a cross-script ownership error.

function getCardData()
    local function firstName(zone)
        if not zone then return nil end
        local objs = zone.getObjects()
        if #objs == 0 then return nil end
        local name = objs[1].getName()
        if name == "" then return nil end
        return name
    end

    local deployZone = getObjectFromGUID(Global.getVar("deploymentCardZone_GUID"))
    local primaryZone = getObjectFromGUID(Global.getVar("primaryCardZone_GUID"))

    return {
        deployment = firstName(deployZone),
        primary    = firstName(primaryZone),
        challenger = firstName(challengerZoneObj),
        p1_sec1    = firstName(secondary11ZoneObj),
        p1_sec2    = firstName(secondary12ZoneObj),
        p2_sec1    = firstName(secondary21ZoneObj),
        p2_sec2    = firstName(secondary22ZoneObj),
    }
end
