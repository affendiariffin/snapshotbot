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
    lay = layouts_meta().get(layout_key(bundle.get("mission_meta") or {}) or "")
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
