# Rebuild the objective AREAS and territory lines in server/static/layouts/layouts_meta.json.
#
# KEEPER, not a throwaway: rapidingress lags GW by ~a week on every balance dataslate, so this
# re-runs each time the archive under docs/reference/rapidingress/ is refreshed. Check the three
# ETags against MANIFEST.json first (see reference_rapidingress) -- if they haven't moved, the
# rebuild reproduces the current file byte-for-byte and there is nothing to do.
#
# WHY (audited 2026-08-04): objective areas were hand-traced and unreliable. 68 of 240 were never
# traced at all -- evPlace then fell back to "near a terrain area" / "in blue's DZ" and never named
# the objective -- and of the 172 that WERE traced, only 91 actually contained their own objective
# marker; the rest were misplaced, some mirrored outright (RE-RE-C's central objective sat at
# x[-3.0,5.1] where the real footprint is x[-5.0,3.1]). So we take all 240 from rapidingress rather
# than only filling the gaps. Verified: RI terrain footprints land on the SVG terrain-layer
# polygons EXACTLY (mean/median/max deviation 0.00 over 183 pieces), which is what pins the
# transform below; and afterwards all 240 markers sit inside their own area.
#
# NOT touched: `terrain`. meta holds 198 merged AREA outlines, RI holds 1103 individual footprints
# -- a different granularity, not a mistrace. Swapping it would change the rendered board.
#
# Transforms -- NOTE THE SIGNS DIFFER, they are not the same convention:
#   RI  -> world:  x = ri_x - 30           z = ri_y - 22     (RI is inches, board coords, y UP)
#   SVG -> world:  x = px/20 - 30          z = 22 - py/20    (viewBox 1200x880, screen coords, y DOWN)
# The SVG form reproduces the existing red_poly/blue_poly to the last decimal. Getting the RI sign
# wrong is nearly invisible on these mirror-symmetric layouts -- a flipped objective still lands
# close to SOME objective -- so the gate below checks IDENTITY (does each marker fall inside the
# area it was matched to), not proximity. Under the wrong sign that gate drops to ~90/240.
#
# RI keys its layouts by its own disposition order (PF-TH-A where we say TH-PF-A), so the index is
# rebuilt with CODE_ORDER rather than trusting `id`.

import json
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from server.zones import CODE_ORDER      # noqa: E402
LAYDIR = os.path.join(ROOT, "server", "static", "layouts")
META = os.path.join(LAYDIR, "layouts_meta.json")
RIJSON = os.path.join(ROOT, "docs", "reference", "rapidingress", "ri_layouts_11e.json")
PPI = 20.0
SIMP_TOL = 0.12          # inches the simplified objective outline may deviate from the raw footprint

def ri_pt(p):
    return (round(p["x"] - 30.0, 3), round(p["y"] - 22.0, 3))


def svg_pt(x, y):
    return (round(x / PPI - 30.0, 3), round(22.0 - y / PPI, 3))


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


def centroid(poly):
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def area(poly):
    a = 0.0
    for i in range(len(poly)):
        x1, z1 = poly[i - 1]
        x2, z2 = poly[i]
        a += x1 * z2 - x2 * z1
    return abs(a) / 2.0


def _seg_dist(p, a, b):
    dx, dz = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dz == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / (dx * dx + dz * dz)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dz))


def _dp(pts, tol):
    if len(pts) < 3:
        return list(pts)
    worst, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = _seg_dist(pts[i], pts[0], pts[-1])
        if d > worst:
            worst, idx = d, i
    if worst <= tol:
        return [pts[0], pts[-1]]
    return _dp(pts[:idx + 1], tol)[:-1] + _dp(pts[idx:], tol)


def simplify(poly, tol=SIMP_TOL):
    # RI footprints are densely sampled (up to ~250 points on one ruin) -- too heavy to ship and
    # to run point-in-poly against every frame. Douglas-Peucker, which measures deviation from the
    # RETAINED chord and so guarantees the outline never moves more than `tol` inches.
    #
    # Do NOT go back to dropping points that look collinear with their immediate neighbours: on a
    # densely-sampled outline consecutive points sit ~0.03" apart, every one of them passes that
    # test, and a 250-point ruin collapses to 5 points. Area survives that (a wrong shape can
    # enclose the right area, ratio was 0.995) which is exactly why it slips through -- the gate
    # has to be max deviation, not area.
    if len(poly) < 4:
        return list(poly)
    closed = poly + [poly[0]]
    out = _dp(closed, tol)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out if len(out) >= 3 else list(poly)


def svg_layer(src, name):
    i = src.find('<g id="%s"' % name)
    if i < 0:
        return ""
    depth, j = 0, i
    while j < len(src):
        if src.startswith("<g", j):
            depth += 1
        elif src.startswith("</g>", j):
            depth -= 1
            if depth == 0:
                return src[i:j + 4]
        j += 1
    return src[i:]


def territory_line(key):
    path = os.path.join(LAYDIR, key + ".svg")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        lay = svg_layer(f.read(), "territory-line-layer")
    m = re.search(r'<line[^>]*x1="([\d.eE+-]+)"[^>]*y1="([\d.eE+-]+)"'
                  r'[^>]*x2="([\d.eE+-]+)"[^>]*y2="([\d.eE+-]+)"', lay)
    if not m:
        return None
    x1, y1, x2, y2 = (float(v) for v in m.groups())
    return [list(svg_pt(x1, y1)), list(svg_pt(x2, y2))]


def main():
    with open(META, encoding="utf-8") as f:
        meta = json.load(f)
    with open(RIJSON, encoding="utf-8") as f:
        ri = {}
        for x in json.load(f):
            codes = sorted(x["matchup"], key=CODE_ORDER.index)
            ri["%s-%s-%s" % (codes[0], codes[1], x["variant"])] = x

    stats = {"layouts": 0, "objs": 0, "filled": 0, "replaced": 0, "worst_match": 0.0,
             "verts_before": 0, "verts_after": 0, "lines": 0, "contained": 0, "max_dev": 0.0, "problems": []}

    for key, lay in sorted(meta.items()):
        src = ri.get(key)
        if not src:
            stats["problems"].append("%s: absent from rapidingress" % key)
            continue
        stats["layouts"] += 1

        # group RI objective footprints by objective number; an objective spanning several
        # footprints keeps the largest (the marker always sits on the main body).
        groups = {}
        for piece in src["terrain"]:
            ob = piece.get("objective")
            if not ob:
                continue
            raw = [list(ri_pt(p)) for p in piece["points"]]
            poly = simplify(raw)
            # fidelity gate: no raw point may sit further than SIMP_TOL from the kept outline.
            dev = 0.0
            for p in raw:
                dev = max(dev, min(_seg_dist(p, poly[i - 1], poly[i]) for i in range(len(poly))))
            stats["max_dev"] = max(stats["max_dev"], dev)
            if dev > SIMP_TOL * 1.5:
                stats["problems"].append(
                    "%s: %s simplified outline moved %.2f in" % (key, piece.get("areaId"), dev))
            g = groups.setdefault(ob.get("number"), [])
            g.append({"poly": poly, "areaId": piece.get("areaId"), "ob": ob})
        cands = []
        for num, g in groups.items():
            g.sort(key=lambda d: area(d["poly"]), reverse=True)
            cands.append(g[0])

        objs = lay.get("objectives") or []
        if len(cands) != len(objs):
            stats["problems"].append(
                "%s: %d RI objectives vs %d in meta" % (key, len(cands), len(objs)))

        # bijective nearest-match on the existing marker point, which already carries the
        # correct red/blue label -- so labels are preserved and only geometry is rebuilt.
        pairs = sorted(
            ((math.hypot(o["x"] - centroid(c["poly"])[0], o["z"] - centroid(c["poly"])[1]), oi, ci)
             for oi, o in enumerate(objs) for ci, c in enumerate(cands)))
        used_o, used_c = set(), set()
        for dist, oi, ci in pairs:
            if oi in used_o or ci in used_c:
                continue
            used_o.add(oi)
            used_c.add(ci)
            o, c = objs[oi], cands[ci]
            stats["objs"] += 1
            stats["worst_match"] = max(stats["worst_match"], dist)
            # identity gate: the existing marker must fall INSIDE the area we are about to
            # attach to it. Distance alone cannot catch a sign flip on a symmetric layout.
            if in_poly(c["poly"], o["x"], o["z"]):
                stats["contained"] += 1
            else:
                stats["problems"].append(
                    "%s: %s marker NOT inside its matched area (%.1f in away)"
                    % (key, o.get("label"), dist))
            if o.get("poly"):
                stats["replaced"] += 1
                stats["verts_before"] += len(o["poly"])
            else:
                stats["filled"] += 1
            stats["verts_after"] += len(c["poly"])
            o["poly"] = c["poly"]
            o["areaId"] = c["areaId"]
            if c["ob"].get("owner"):
                o["owner"] = c["ob"]["owner"]      # attacker / defender, per the layout
        for oi, o in enumerate(objs):
            if oi not in used_o:
                stats["problems"].append("%s: %s got no area" % (key, o.get("label")))

        line = territory_line(key)
        if line:
            lay["territory_line"] = line
            stats["lines"] += 1
        else:
            stats["problems"].append("%s: no territory line in SVG" % key)

    ok = (stats["layouts"] == len(meta) and stats["objs"] == stats["contained"]
          and stats["lines"] == len(meta) and not stats["problems"])

    print("layouts rebuilt      : %d / %d" % (stats["layouts"], len(meta)))
    print("objective areas      : %d  (%d newly filled, %d replaced)"
          % (stats["objs"], stats["filled"], stats["replaced"]))
    print("marker inside area   : %d / %d   <-- identity gate"
          % (stats["contained"], stats["objs"]))
    print("territory lines      : %d" % stats["lines"])
    print("worst marker->area   : %.2f in" % stats["worst_match"])
    print("max outline deviation: %.3f in  (tol %.2f)   <-- shape gate"
          % (stats["max_dev"], SIMP_TOL))
    print("vertices (replaced)  : %d -> %d" % (stats["verts_before"], stats["verts_after"]))
    if stats["problems"]:
        print("\nPROBLEMS (%d):" % len(stats["problems"]))
        for p in stats["problems"][:25]:
            print("   " + p)
    if not ok:
        print("\nABORTED -- nothing written.")
        return 1

    with open(META, "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print("\nOK -- written, %d bytes" % os.path.getsize(META))
    return 0


if __name__ == "__main__":
    sys.exit(main())
