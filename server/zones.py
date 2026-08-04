# Layout metadata + deployment-zone team backfill.
# Lives outside app.py so db.py can enrich bundles without a circular import.
import json
import os
import re

DISP_CODES = {
    "Take and Hold": "TH", "Purge the Foe": "PF", "Disruption": "DI",
    "Reconnaissance": "RE", "Priority Assets": "PA",
}
# Map-card-name abbreviations (LCT deck naming) — fallback when dispositions are unset.
MAP_ABBREVS = {"TnH": "TH", "PtF": "PF", "Dis": "DI", "Rec": "RE", "Recon": "RE", "PA": "PA"}
CODE_ORDER = ["TH", "PF", "DI", "RE", "PA"]

_layouts_meta = None


def layouts_meta():
    global _layouts_meta
    if _layouts_meta is None:
        path = os.path.join(os.path.dirname(__file__), "static", "layouts", "layouts_meta.json")
        with open(path, encoding="utf-8") as f:
            _layouts_meta = json.load(f)
    return _layouts_meta


def layout_key(meta):
    codes = []
    for side in ("red_disposition", "blue_disposition"):
        codes.append(DISP_CODES.get(meta.get(side) or ""))
    map_name = meta.get("map") or ""
    if not all(codes):
        found = [MAP_ABBREVS[a] for a in re.findall(r"\b(TnH|PtF|Dis|Recon|Rec|PA)\b", map_name)]
        if len(found) >= 2:
            codes = found[:2]
    if not all(codes):
        return None
    codes.sort(key=CODE_ORDER.index)
    m = re.search(r"\b([123])\b", map_name)
    letter = "ABC"[int(m.group(1)) - 1] if m else "A"
    key = f"{codes[0]}-{codes[1]}-{letter}"
    return key if key in layouts_meta() else None


# --- Layout inference from the table (loose cards) ---------------------------
# mission_meta is frozen at session-start; if the map card / dispositions weren't
# set yet it's wrong (Fendi's Purge-vs-Recon game, 2026-07-09, froze as PF-DI-A
# with an empty map). The cards physically on the table are ground truth: the map
# card ("PtF vs Recon 2 - Dawn of War") gives BOTH the disposition pair and the
# A/B/C letter; the primary-mission cards (Consecrate / Triangulation) pin the
# pair on their own when no map card is face-up.
_ABBR = {a.lower(): c for a, c in MAP_ABBREVS.items()}
_MAP_CARD_RE = re.compile(
    r"\b(TnH|PtF|Dis|Recon|Rec|PA)\s+vs\s+(TnH|PtF|Dis|Recon|Rec|PA)\s+([123])\b", re.I)
_primary_pairs = None


def _key_from_map_card(name):
    m = _MAP_CARD_RE.search(name or "")
    if not m:
        return None
    codes = [_ABBR.get(m.group(1).lower()), _ABBR.get(m.group(2).lower())]
    if not all(codes):
        return None
    codes.sort(key=CODE_ORDER.index)
    key = f"{codes[0]}-{codes[1]}-{'ABC'[int(m.group(3)) - 1]}"
    return key if key in layouts_meta() else None


def _primary_pair_map():
    # frozenset(primary mission names) -> disposition pair ("PF-RE"). Collision-free
    # across the layout library — each pairing has a unique pair of primaries.
    global _primary_pairs
    if _primary_pairs is None:
        _primary_pairs = {}
        for k, v in layouts_meta().items():
            prims = frozenset(str(x).strip() for x in (v.get("missions") or {}).values())
            if len(prims) >= 2:
                _primary_pairs[prims] = "-".join(k.split("-")[:2])
    return _primary_pairs


def _dpair_from_primaries(loose):
    pairs = _primary_pair_map()
    universe = set().union(*pairs.keys()) if pairs else set()
    present = {re.sub(r"\s+[123]$", "", (n or "").strip()) for n in loose}
    present &= universe
    hits = [dp for prims, dp in pairs.items() if prims <= present]
    return hits[0] if len(hits) == 1 else None


def _at_scoreboard(c):
    # The mod's own "current map" display: the chosen map card sits by the
    # scoresheet, off-mat at (~60.8–65, 0) in every recorded session. Selection
    # spreads live ON the mat (|x| <= 31) and discard piles at (62.5, ±16.6),
    # so a map card inside this box is the played one — source of truth.
    try:
        return 45 <= float(c.get("x")) <= 80 and abs(float(c.get("z"))) <= 8
    except (TypeError, ValueError):
        return False


def _loose_stats(bundle):
    # name -> [snapshot count, last snapshot id, scoreboard-slot count].
    # Persistence matters: map selection leaves EVERY candidate map card
    # face-up for a frame (session 45 caught all three PtF-vs-Recon maps plus
    # saved-table variants in one setup snapshot), so loose-card inference must
    # weigh where and how long a card sat — never a flat whole-session union.
    stats = {}
    for s in bundle.get("snapshots") or []:
        sid = s.get("id") or 0
        for c in (s.get("cards") or {}).get("loose") or []:
            if isinstance(c, dict) and c.get("n"):
                st = stats.setdefault(c["n"], [0, 0, 0])
                st[0] += 1
                st[1] = max(st[1], sid)
                if _at_scoreboard(c):
                    st[2] += 1
    return stats


def layout_key_from_bundle(bundle):
    # Prefer what's on the table over the frozen session-start meta. The map
    # card at the SCOREBOARD SLOT wins outright; among equals, longest-seen
    # (ties: latest, then name). First-match over a set was a per-process coin
    # flip whenever setup left several map cards visible, and it fed
    # thumb/OG/backfill the wrong layout.
    stats = _loose_stats(bundle)
    best = best_rank = None
    for n, (count, last, slot) in stats.items():
        k = _key_from_map_card(n)          # map card: dispositions + letter
        if k and (best_rank is None or (slot, count, last, n) > best_rank):
            best, best_rank = k, (slot, count, last, n)
    if best:
        return best
    # Primaries: dispositions only. Transient setup cards can complete a bogus
    # pairing, so cards seen in >1 snapshot get first say; all cards only if
    # that stays inconclusive.
    persistent = {n for n, st in stats.items() if st[0] > 1}
    dpair = ((_dpair_from_primaries(persistent) if persistent else None)
             or _dpair_from_primaries(set(stats)))
    if dpair:
        d = re.search(r"\b([123])\b", (bundle.get("mission_meta") or {}).get("map") or "")
        for n in ([d.group(1)] if d else []) + ["1", "2", "3"]:
            key = f"{dpair}-{'ABC'[int(n) - 1]}"
            if key in layouts_meta():
                return key
    return layout_key(bundle.get("mission_meta") or {})


def _in_poly(x, z, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, zi = poly[i]
        xj, zj = poly[j]
        if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / (zj - zi) + xi:
            inside = not inside
        j = i
    return inside


def backfill_teams(bundle, early=None):
    # Fallback for tokens spawned post-deployment: a unit standing inside a
    # deployment zone during round 0 belongs to that zone's player. Only fires
    # once real claims (GMNotes/drop/reserve-board) fix the board orientation —
    # with zero claims red-vs-blue is a coin flip, and a wrong colour is worse
    # than an honest grey (Fendi, 2026-07-05). Zone shapes come from the layout
    # SVGs (red_poly/blue_poly, world inches, red on the SVG's baked side).
    lay = layouts_meta().get(layout_key_from_bundle(bundle) or "")
    if not lay or not lay.get("red_poly") or not lay.get("blue_poly"):
        return bundle
    if early is None:
        early = [s for s in bundle["snapshots"] if (s.get("round") or 0) < 2]
    rz = lay["red_zone"]
    dot = votes = 0
    for s in early:
        for m in s.get("models") or []:
            if not m.get("t") or m.get("v"):
                continue
            dot += (m["x"] * rz[0] + m["z"] * rz[1]) * (1 if m["t"] == "red" else -1)
            votes += 1
    if votes == 0:
        return bundle
    flip = -1 if dot < 0 else 1
    # Unclaimed round-0 positions, keyed by yellowscribe unit — or, for models
    # without one, by name (no stable per-model id exists across frames; a name
    # claims a team only when ALL its round-0 holders sit in the same zone).
    pos, npos = {}, {}
    for s in early:
        if (s.get("round") or 0) != 0:
            continue
        for m in s.get("models") or []:
            if m.get("t") or m.get("v"):
                continue
            if m.get("u"):
                pos.setdefault(m["u"], []).append((m["x"], m["z"]))
            elif m.get("n"):
                npos.setdefault(m["n"].lower(), []).append((m["x"], m["z"]))
    claim = {}
    for u, pts in pos.items():
        cx = flip * sum(p[0] for p in pts) / len(pts)
        cz = flip * sum(p[1] for p in pts) / len(pts)
        if _in_poly(cx, cz, lay["red_poly"]):
            claim[u] = "red"
        elif _in_poly(cx, cz, lay["blue_poly"]):
            claim[u] = "blue"
    nclaim = {}
    for name, pts in npos.items():
        sides = {("red" if _in_poly(flip * x, flip * z, lay["red_poly"]) else
                  "blue" if _in_poly(flip * x, flip * z, lay["blue_poly"]) else None)
                 for x, z in pts}
        if len(sides) == 1 and None not in sides:
            nclaim[name] = sides.pop()
    if not claim and not nclaim:
        return bundle
    for s in bundle["snapshots"]:
        for m in s.get("models") or []:
            if m.get("t"):
                continue
            if m.get("u") in claim:
                m["t"] = claim[m["u"]]
            elif not m.get("u") and (m.get("n") or "").lower() in nclaim:
                m["t"] = nclaim[(m.get("n") or "").lower()]
    return bundle


# --- Reserves-board zones -------------------------------------------------------------
# The Reinforcements and Reserves board is PRINTED with labelled zones, so a model's
# position on it declares its reserve category outright -- no inference (Fendi, 2026-07-24).
# The bands are FULL-WIDTH ROWS: rz alone picks the category, rx only picks the transport
# slot within the two TRANSPORTS rows. Do not treat the printed label text as the extent of
# a zone; a model far left or right of the "STRATEGIC RESERVES" caption is still in that row.
#
# Calibrated 2026-07-24 against a deliberate placement (12 models, all 4 categories), and
# corroborated by obe_a7Ek behaviour: Vahl + Paragons (deep struck into the enemy DZ) sat at
# LOW raw z, the Dominions (verified disembarking beside hulls) sat at HIGH raw z -- same
# direction, so rz runs top -> bottom.
#   rz: 0 = DEEP STRIKE edge -> 1 = far TRANSPORTS edge, four equal rows
#   rx: slot 1 sits at HIGH rx, i.e. MIRRORED vs the printed 1..4 numbering
#
# Both bands above are calibrated on RED. Each board faces its own player, so blue's is
# red's turned 180 deg -- and the token normalises rx/rz against the board's WORLD-axis
# bounding box, which cannot see that rotation. Blue therefore arrives mirrored on both
# axes and must be un-rotated here (Fendi, 2026-08-04: JulxL-gk showed blue's transported
# units sitting in the DEEP STRIKE + STRATEGIC RESERVES rows, i.e. rows 3 and 2 read
# upside down). Doing it server-side keeps one calibration and fixes past recordings.
RESERVE_ROWS = ("DEEP STRIKE", "STRATEGIC RESERVES", "TRANSPORT", "TRANSPORT")


def reserve_zone(rx, rz, side=None):
    if rx is None or rz is None:
        return None
    if side == "blue":
        rx, rz = 1 - rx, 1 - rz
    row = min(3, max(0, int(rz * 4)))
    if row < 2:
        return RESERVE_ROWS[row]
    col = 3 - min(3, max(0, int(rx * 4)))
    return f"TRANSPORT {(row - 2) * 4 + col + 1}"


def tag_reserve_zones(bundle):
    """Attach `zone` to every reserve model that carries board coordinates.

    Computed once here so the viewer, the analyze-game cruncher and the offline download
    all read the same label -- and so the thresholds can be corrected without rebuilding
    and re-spawning the token.
    """
    for snap in bundle.get("snapshots") or []:
        for m in snap.get("models") or []:
            z = reserve_zone(m.get("rx"), m.get("rz"), m.get("t"))
            if z:
                m["zone"] = z
    return bundle
