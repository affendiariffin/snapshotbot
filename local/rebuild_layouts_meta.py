# Rebuild server/static/layouts/layouts_meta.json -- the file everything MEASURES against.
#
# KEEPER, offline, re-runnable. Source is now **GW's own Warhammer Event Companion**, via
# docs/reference/gw/gw_layouts.json (local/extract_gw_layouts.py). rapidingress is retired: it
# lagged GW by ~a week on every dataslate, stopped tracking the CA26 layouts, and -- discovered
# during the migration -- carried the wrong central-objective count on 6 layouts
# (PF-DI-A/B/C, TH-PF-A/B/C say 1, GW prints 2) in every version we ever archived.
#
# WHAT THIS FILE FEEDS, AND WHY THE FIELD NAMES ARE A CONTRACT
# zones.py reads this at runtime for every placement call: red_poly/blue_poly (deployment and
# the team backfill), red_zone (flip detection), territory_line (a DIAGONAL split on 40 of 45
# layouts, so never a board-axis test), terrain/terrain_areas (what evPlace measures to) and
# objectives. bake_threat_maps.py reads the zone polygons.
#
# ⚠ objective["label"] IS PARSED, NOT JUST PRINTED. zone_tokens() decides whether a home
# objective is yours or the enemy's with label.startswith("red"/"blue"), and that feeds
# relevant_secondaries(). Rename those strings and the mission-scoring tokens go quietly
# missing -- no error, just secondaries that stop being flagged. The vocabulary is fixed:
#     home       "red's home objective"        / "blue's home objective"
#     expansion  "red's expansion objective"   / "blue's expansion objective"
#     central    "the central objective" when a layout has one; otherwise
#                "the red-side central objective" / "the blue-side central objective"
# Side is decided by the TERRITORY LINE, using the same cross-product test zones._territory()
# uses, so the label and the runtime answer can never disagree.
#
# PRESERVED, NOT REBUILT: label, missions, attackerEdge. Those are mission metadata, not
# geometry -- `missions` in particular is what layout_key_from_bundle() matches primary cards
# against, so losing it silently breaks layout inference from the table.
#
# GATES (all of them exist because a weaker check passed while the data was wrong):
#   IDENTITY  every objective must resolve to its OWN terrain area, bijectively. Distance to
#             SOME objective stays small under a sign flip on these near-symmetric boards.
#   SHAPE     max deviation of the simplified outline, never area -- area survives
#             over-simplification, a 250-point ruin collapsed to 5 points at ratio 0.995.
#   LABEL     every home/expansion label starts with the owner, per the contract above.
#   CARRIED   all 45 keep label/missions/attackerEdge.
#
# `stale_dataslate` is GONE. It existed to flag layouts a dataslate had revised while
# rapidingress still served the old geometry; with a GW-native source there is no lag to flag.
# zones.py's reader is harmless when the key is absent (`lay.get`).

import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import gw_pieces as G                                              # noqa: E402

LAYDIR = os.path.join(ROOT, "server", "static", "layouts")
META = os.path.join(LAYDIR, "layouts_meta.json")
GWJSON = os.path.join(ROOT, "docs", "reference", "gw", "gw_layouts.json")

SIMP_TOL = 0.12          # in: how far a simplified outline may move from the traced one
CARRY = ("label", "missions", "attackerEdge")


def centroid(poly):
    return [round(sum(p[0] for p in poly) / len(poly), 2),
            round(sum(p[1] for p in poly) / len(poly), 2)]


def poly_area(poly):
    a = 0.0
    for i in range(len(poly)):
        x1, z1 = poly[i - 1]
        x2, z2 = poly[i]
        a += x1 * z2 - x2 * z1
    return abs(a) / 2.0


def in_poly(poly, x, z):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, zi = poly[i]
        xj, zj = poly[j]
        if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / (zj - zi) + xi:
            inside = not inside
        j = i
    return inside


def edge_dist(poly, x, z):
    if in_poly(poly, x, z):
        return 0.0
    return min(G._seg_dist((x, z), poly[i - 1], poly[i]) for i in range(len(poly)))


def side_of(line, red_zone, x, z):
    """red / blue, by the same cross-product test zones._territory() runs at runtime."""
    (x1, z1), (x2, z2) = line
    cross = (x2 - x1) * (z - z1) - (z2 - z1) * (x - x1)
    ref = (x2 - x1) * (red_zone[1] - z1) - (z2 - z1) * (red_zone[0] - x1)
    if cross == 0 or ref == 0:
        return None
    return "red" if (cross > 0) == (ref > 0) else "blue"


def label_for(kind, owner, n_central):
    if kind == "central":
        return ("the central objective" if n_central < 2
                else "the %s-side central objective" % owner)
    return "%s's %s objective" % (owner, kind)


def main():
    with open(GWJSON, encoding="utf-8") as f:
        gw = json.load(f)
    with open(META, encoding="utf-8") as f:
        old = json.load(f)

    problems = []
    stats = collections.Counter()
    dev_max = 0.0
    out = {}

    for key, lay in sorted(gw.items()):
        prev = old.get(key)
        if not prev or any(prev.get(c) is None for c in CARRY):
            problems.append("%s: no carried mission metadata in the previous meta" % key)
            continue

        areas = []
        for p in lay["terrain_areas"]:
            q = G.simplify_ring(p, SIMP_TOL)
            dev_max = max(dev_max, G.ring_deviation(p, q))
            areas.append(q)
        stats["terrain"] += len(areas)

        groups = lay["area_groups"]
        terrain_areas = [{"id": "%s-T%02d" % (key, gi + 1),
                          "polys": [areas[i] for i in g]}
                         for gi, g in enumerate(groups)]
        stats["areas"] += len(terrain_areas)

        red_zone = centroid(lay["red_poly"])
        line = lay["territory_line"]
        n_central = sum(1 for o in lay["objectives"] if o["kind"] == "central")

        objs, claimed = [], {}
        for o in lay["objectives"]:
            x, z = o["at"]
            owner = o.get("owner") or side_of(line, red_zone, x, z)
            if owner is None:
                problems.append("%s: cannot place %s objective on a side" % (key, o["kind"]))
                continue
            gi = min(range(len(groups)),
                     key=lambda t: min(edge_dist(areas[i], x, z) for i in groups[t]))
            d = min(edge_dist(areas[i], x, z) for i in groups[gi])
            if gi in claimed:
                problems.append("%s: %s and %s objectives both resolve to area %s"
                                % (key, claimed[gi], o["kind"], terrain_areas[gi]["id"]))
            claimed[gi] = o["kind"]
            best = max((areas[i] for i in groups[gi]), key=poly_area)
            rec = {"x": round(x, 2), "z": round(z, 2), "kind": o["kind"],
                   "label": label_for(o["kind"], owner, n_central),
                   "poly": best, "areaId": terrain_areas[gi]["id"]}
            if o.get("restored"):
                rec["restored"] = True
                stats["restored"] += 1
            objs.append(rec)
            stats["objs"] += 1
            stats["worst_obj_dist"] = max(stats["worst_obj_dist"], int(d * 100))

            # LABEL GATE -- zone_tokens() reads these prefixes.
            if o["kind"] in ("home", "expansion") and not rec["label"].startswith(("red", "blue")):
                problems.append("%s: %s label %r does not start with an owner"
                                % (key, o["kind"], rec["label"]))

        rec = {c: prev[c] for c in CARRY}
        rec.update({"red_poly": lay["red_poly"], "blue_poly": lay["blue_poly"],
                    "red_zone": red_zone, "territory_line": line,
                    "terrain": areas, "terrain_areas": terrain_areas, "objectives": objs})
        out[key] = rec

    if len(out) != 45:
        problems.append("expected 45 layouts, built %d" % len(out))
    if dev_max > SIMP_TOL * 1.5:
        problems.append("simplification moved an outline %.3f in" % dev_max)

    print("layouts rebuilt      : %d" % len(out))
    print("terrain footprints   : %d  grouped into %d areas" % (stats["terrain"], stats["areas"]))
    print("objectives           : %d  (%d restored)" % (stats["objs"], stats["restored"]))
    print("worst objective->area: %.2f in  (GW nudges a badge clear of its own artwork)"
          % (stats["worst_obj_dist"] / 100.0))
    print("max outline deviation: %.3f in  (tol %.2f)   <-- shape gate" % (dev_max, SIMP_TOL))
    print("source               : GW Warhammer Event Companion (rapidingress retired)")
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems[:25]:
            print("   " + p)
        print("\nABORTED -- nothing written.")
        return 1

    with open(META, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print("\nOK -- written, %d bytes" % os.path.getsize(META))
    return 0


if __name__ == "__main__":
    sys.exit(main())
