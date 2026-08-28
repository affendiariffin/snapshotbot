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


def flip_sign(bundle, lay, early=None):
    """+1, or -1 when the players sat the other way round from the side the layout SVG bakes in.
    The viewer decides this the same way in maybeFlip(); anything server-side that compares a
    recorded position against layout geometry has to apply it too, or every zone comes out
    mirrored. None = not enough claimed models to call it yet."""
    rz = (lay or {}).get("red_zone")
    if not rz:
        return None
    if early is None:
        early = [s for s in bundle.get("snapshots") or [] if (s.get("round") or 0) < 2]
    dot = votes = 0
    for s in early:
        for m in s.get("models") or []:
            if not m.get("t") or m.get("v"):
                continue
            dot += (m["x"] * rz[0] + m["z"] * rz[1]) * (1 if m["t"] == "red" else -1)
            votes += 1
    if votes == 0:
        return None
    return -1 if dot < 0 else 1


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
    flip = flip_sign(bundle, lay, early)
    if flip is None:
        return bundle
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


# --- Zone resolution -------------------------------------------------------------------
# One place that names WHERE something is, so the viewer, the analyze-game cruncher and the
# offline download all read the same label (same reasoning as tag_reserve_zones). Replaces the
# viewer's client-side evPlace.
#
# Everything measures to an AREA, never to a point: in 11e an objective IS the terrain piece, it
# has no centre, and some are triangles or carry protruding snubs a model can legally stand on
# (Fendi, 2026-08-04). The one exception is Centre Ground, whose card really does measure to the
# centre of the battlefield.
#
# Predicates are the 40k measurement vocabulary and nothing finer -- "wholly within", "within",
# "near". `radius` is the model's base radius, or its hull half-extent for a FRAME model, which
# per Core Rules 17.02 is measured "to and from the closest point on that model (so not
# necessarily from its base, if it has one)" -- so FRAME is decided by the KEYWORD, never by
# whether a base disc happened to be measured. Markers pass radius=0.
BOARD_HX, BOARD_HZ = 30.0, 22.0     # 60x44 battlefield, confirmed by LCT's own zoneScale
NEAR_IN = 3.0                       # the usual 40k slack for "near"
CENTRE_GROUND_IN = 3.0              # Centre Ground: friendly presence radius (BOTH tiers)
CENTRE_GROUND_FAR_IN = 6.0          # ... and the 5VP enemy-exclusion radius
INGRESS_IN = 6.0                    # Ingress Move 20.04 set-up distance
QUARTER_EXCL_IN = 6.0               # table quarters ignore the middle (Engage on All Fronts)
AMBIG_IN = 1.0                      # two objectives this close in distance = "between", not "near X"


def _edge_dist(poly, x, z):
    """Distance from a point to a polygon BOUNDARY, whether the point is inside or outside."""
    best = float("inf")
    for i in range(len(poly)):
        x1, z1 = poly[i - 1]
        x2, z2 = poly[i]
        dx, dz = x2 - x1, z2 - z1
        den = dx * dx + dz * dz
        t = 0.0 if den == 0 else max(0.0, min(1.0, ((x - x1) * dx + (z - z1) * dz) / den))
        best = min(best, ((x - (x1 + t * dx)) ** 2 + (z - (z1 + t * dz)) ** 2) ** 0.5)
    return best


def relate(poly, x, z, radius=0.0, near=NEAR_IN):
    """Where a footprint of `radius` sits relative to an area. None = not in contact."""
    if not poly:
        return None
    edge = _edge_dist(poly, x, z)
    if _in_poly(x, z, poly):
        return "wholly within" if edge >= radius else "within"
    if edge <= radius:
        return "within"
    return "near" if edge <= near else None


def _territory(lay, x, z):
    """Which half the point sits in, split by the SVG's territory line. That line is often
    DIAGONAL, so this is deliberately not a board-axis test."""
    ln = lay.get("territory_line")
    rz = lay.get("red_zone")
    if not ln or not rz:
        return None
    (x1, z1), (x2, z2) = ln
    cross = (x2 - x1) * (z - z1) - (z2 - z1) * (x - x1)
    ref = (x2 - x1) * (rz[1] - z1) - (z2 - z1) * (rz[0] - x1)
    if cross == 0 or ref == 0:
        return None
    return "red" if (cross > 0) == (ref > 0) else "blue"


def _quarter(lay, x, z):
    """Table quarter, named by the home/expansion objective standing in it. Board centre lines,
    minus a 6in middle exclusion (Fendi: the Engage on All Fronts / Recon-vs-Take-and-Hold shape)."""
    if (x * x + z * z) ** 0.5 <= QUARTER_EXCL_IN:
        return None
    best, bd = None, float("inf")
    for o in lay.get("objectives") or []:
        if o.get("kind") not in ("home", "expansion"):
            continue
        if (o["x"] > 0) != (x > 0) or (o["z"] > 0) != (z > 0):
            continue
        d = _edge_dist(o["poly"], x, z) if o.get("poly") else 1e9
        if d < bd:
            bd, best = d, o
    return ("the table quarter near " + best["label"]) if best else None


def place(x, z, lay, side=None, rnd=None, radius=0.0):
    """Every zone a point touches, most specific first. `side` is whose model/marker it is (for
    friendly-vs-enemy centre ground), `rnd` the battle round (for ingress eligibility)."""
    if not lay:
        return {}
    out = {}
    # `stale_dataslate` marked a layout whose geometry lagged the printed board, back when
    # layouts came from a third-party mirror. They come from GW's own Event Companion now
    # (2026-08-28), so nothing writes the key any more. The READ stays: a replay downloaded
    # before the migration carries its own embedded meta and can still have it.
    if lay.get("stale_dataslate"):
        out["provisional"] = lay["stale_dataslate"]

    ranked = []
    for o in lay.get("objectives") or []:
        how = relate(o.get("poly"), x, z, radius)
        if how:
            tier = ("wholly within", "within", "near").index(how)
            ranked.append((tier, _edge_dist(o["poly"], x, z), o, how))
    ranked.sort(key=lambda r: (r[0], r[1]))          # closest wins, never list order
    if ranked:
        tier, dist, o, how = ranked[0]
        # "near X" claims proximity to X IN PARTICULAR. Between two objectives that claim is
        # false whichever one you name -- and that is exactly where the board centre sits on the
        # 15 two-central layouts, where the middle is no-man's-land rather than an objective
        # (Fendi, 2026-08-04). Measured there, the two centrals are 0.01-0.36in apart in distance,
        # so picking by list order silently named the wrong one. Claim neither instead.
        tied = (how == "near" and len(ranked) > 1 and ranked[1][0] == tier
                and ranked[1][1] - dist <= AMBIG_IN)
        if tied:
            out["between"] = [o.get("label"), ranked[1][2].get("label")]
        else:
            out["objective"] = {"label": o.get("label"), "kind": o.get("kind"), "how": how}

    for a in lay.get("terrain_areas") or []:
        hit = None
        for p in a["polys"]:
            how = relate(p, x, z, radius)
            if how and (hit is None or how != "near"):
                hit = how
        if hit:
            out.setdefault("terrain_area", {"id": a["id"], "how": hit})

    for who, key in (("red", "red_poly"), ("blue", "blue_poly")):
        how = relate(lay.get(key), x, z, radius)
        if how and how != "near":
            out["dz"] = {"side": who, "how": how}

    terr = _territory(lay, x, z)
    if terr:
        out["territory"] = terr
    # No-man's-land is not drawn in the layout -- the SVG colour-codes only the two deployment
    # zones (red rgba(255,74,74,.1) / blue rgba(74,158,255,.1)) and the territory split is the
    # dashed line. So NML is the complement: on the board, in neither DZ. Fendi's taxonomy has
    # territory = own DZ + own no-man's-land, so the two together name the half precisely.
    if not out.get("dz") and abs(x) <= BOARD_HX and abs(z) <= BOARD_HZ:
        out["no_mans_land"] = True
    q = _quarter(lay, x, z)
    if q:
        out["quarter"] = q

    # Centre Ground measures to the CENTRE of the battlefield, not to an area -- the one point
    # measurement in this file. 3in is the friendly presence radius for BOTH tiers, 6in is the
    # 5VP enemy-exclusion radius (card read 2026-08-04; the shorthand "friendly 3, enemy 6" has
    # the friendly radius wrong for the 3VP tier).
    dc = max(0.0, (x * x + z * z) ** 0.5 - radius)
    if dc <= CENTRE_GROUND_IN:
        out["centre_ground"] = "inner"
    elif dc <= CENTRE_GROUND_FAR_IN:
        out["centre_ground"] = "outer"

    # Ingress Move 20.04: set up WHOLLY WITHIN 6in of one or more battlefield edges, and before
    # the third battle round no model may be within the OPPONENT's deployment zone. The other
    # half of the rule (">8in horizontally from all enemy units") depends on where the enemy is
    # standing, so it is not a fixed zone and is not modelled here.
    edge = min(BOARD_HX - abs(x), BOARD_HZ - abs(z))
    if edge + radius <= INGRESS_IN:
        out["edge_band"] = True          # Outflank only needs the band; ingress needs it LEGAL
        in_foe_dz = bool(out.get("dz")) and side and out["dz"]["side"] != side
        out["ingress"] = not (rnd is not None and rnd < 3 and in_foe_dz)

    return out


def describe(p):
    """Render place() the way the callouts read: most specific first, container last."""
    if not p:
        return ""
    parts = []
    o = p.get("objective")
    if o:
        parts.append(("near " if o["how"] == "near" else "on ") + o["label"])
    elif p.get("between"):
        parts.append("between %s and %s" % tuple(p["between"]))
    if p.get("dz"):
        parts.append("in %s's DZ" % p["dz"]["side"])
    elif p.get("ingress"):
        parts.append("in the Strategic Reserves ingress zone")
    if p.get("terrain_area"):
        parts.append(("near" if p["terrain_area"]["how"] == "near" else "in") + " a terrain area")
    if p.get("centre_ground") == "inner":
        parts.append("on the centre ground")
    if not parts:
        if p.get("quarter"):
            parts.append(p["quarter"])
        elif p.get("no_mans_land") and p.get("territory"):
            parts.append("in %s's no-man's-land" % p["territory"])
        elif p.get("territory"):
            parts.append("%s territory" % p["territory"])
    out = ", ".join(parts)
    if out and p.get("provisional"):
        out += " (pre-dataslate layout)"
    return out


def tag_marker_zones(bundle):
    """Attach `zone` to every marker so the viewer stops computing placement client-side."""
    lay = layouts_meta().get(layout_key_from_bundle(bundle) or "")
    if not lay:
        return bundle
    # Recorded positions are in table space; the layout is in SVG space. Without this the zones
    # come out mirrored on every game where the players sat the other way round.
    flip = flip_sign(bundle, lay)
    if flip is None:
        return bundle
    for snap in bundle.get("snapshots") or []:
        rnd = snap.get("round") or 0
        for mk in snap.get("markers") or []:
            if mk.get("x") is None:
                continue
            pl = place(flip * mk["x"], flip * mk["z"], lay, side=mk.get("t"), rnd=rnd)
            mk["zone"] = describe(pl)
            held = (snap.get("cards") or {}).get((mk.get("t") or "") + "_secondary") or []
            rel = relevant_secondaries(pl, mk.get("t"), held)
            if rel:
                mk["relevant_to"] = rel
    return bundle


# --- Mission context ------------------------------------------------------------------
# Which of a side's HELD secondaries a position is RELEVANT TO. Zone tokens per card are transcribed
# from the official card faces (the renders sb_card_images already holds, harvested by
# local/gdm_secondaries.py) into mission_scoring.json, which is contract-governed -- run
# framework/contracts/validate.py after editing it.
#
# "Relevant to", never "scores". Three of these cards score for being OUTSIDE the zone they name:
#   Beacon    3VP if the beacon unit is NOT within your DZ, 5VP if NOT within your territory
#   Outflank  within 6" of a battlefield edge and NOT within your territory
#   Plunder   a unit within a terrain area NOT within your territory
# so a token marks relevance and the reader draws the conclusion. Scoring also depends on unit
# keywords (AIRCRAFT and battle-shocked are excluded throughout), on control, and on actions
# having been started -- none of which a position alone can settle.
_mission_scoring = None


def mission_scoring():
    global _mission_scoring
    if _mission_scoring is None:
        path = os.path.join(os.path.dirname(__file__), "static", "mission_scoring.json")
        try:
            with open(path, encoding="utf-8") as f:
                _mission_scoring = json.load(f)
        except (OSError, ValueError):
            _mission_scoring = {}
    return _mission_scoring


def _card_key(name):
    return re.sub(r"^\d+[.)]\s*", "", str(name or "")).strip().lower()


def zone_tokens(p, side=None):
    """place() result -> the zone tokens mission_scoring.json keys its `zones` lists on."""
    out = set()
    o = p.get("objective")
    if o:
        out.add("objective")
        kind, label = o.get("kind"), (o.get("label") or "").lower()
        if kind == "expansion":
            out.add("objective:expansion")
        elif kind == "home" and side:
            owner = "red" if label.startswith("red") else "blue" if label.startswith("blue") else None
            if owner:
                out.add("objective:home:own" if owner == side else "objective:home:enemy")
    if p.get("dz") and side:
        out.add("dz:own" if p["dz"]["side"] == side else "dz:enemy")
    if p.get("territory") and side:
        out.add("territory:own" if p["territory"] == side else "territory:enemy")
    if p.get("terrain_area"):
        out.add("terrain")
    if p.get("quarter"):
        out.add("quarter")
    if p.get("centre_ground"):
        out.add("centre")
    if p.get("edge_band"):
        out.add("edge")
    if p.get("no_mans_land"):
        out.add("nml")
    return out


def relevant_secondaries(p, side, held):
    """Names of `held` secondaries whose card condition turns on a zone this position touches --
    i.e. the cards that CARE about where this is, which is a narrower claim than scoring. `held`
    is that side's secondary cards at this frame; a card not in hand is not relevant, the same
    gate killTag() uses."""
    secs = (mission_scoring() or {}).get("secondaries") or {}
    toks = zone_tokens(p, side)
    if not toks:
        return []
    hits = []
    for raw in held or []:
        k = _card_key(raw)
        z = (secs.get(k) or {}).get("zones")
        if z and toks.intersection(z):
            hits.append(k)
    return sorted(set(hits))
