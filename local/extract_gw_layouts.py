# Extract all 45 official 11e terrain layouts straight out of GW's Warhammer Event Companion PDF.
#
# KEEPER, not a throwaway: this replaces rapidingress.com as the source of layout geometry.
# RI lagged GW by ~a week on every balance dataslate and has now stopped tracking the CA26
# layouts altogether; the Event Companion is GW's own document and ships WITH the dataslate,
# so there is no lag left to work around. Re-run it whenever a new companion lands.
#
# Output: docs/reference/gw/gw_layouts.json  (+ MANIFEST.json recording the source PDF + md5)
# Consumed by: rebuild_layouts_meta.py (objective areas / terrain / territory lines) and the
# SVG emitter. Nothing here writes into server/static -- this stage is pure extraction.
#
# THE BOARD RECT, AND WHY IT IS NOT THE FRAME STROKE
# Every one of the 45 layout pages places a 1-inch grid mat raster at the SAME page rect,
# (127.691, 276.662)-(468.448, 741.499) pt, and that raster IS the 44x60 battlefield: its
# aspect is 1.36413 against a true 60/44 = 1.36364, i.e. 0.036% out, about half a pixel.
# Calibrating on it lands the red DZ inner edge at 20.02in against a printed 20in and the DZ
# width at +-21.96in against +-22in -- good to 0.05in.
# Do NOT calibrate on the drawn board frame (the width-2.4 dark stroke): it is ~0.19in short
# in v, which is why the DZ fills look inset at the outer board edge. The frame is artwork,
# the grid mat is the measurement.
#
# ORIENTATION -- the one transform, and it is a rotation not a flip
# GW draws the board PORTRAIT, 44in wide x 60in tall, with the ATTACKER (red) at the TOP.
# Snapshotbot is landscape 60x44 with red on the LEFT. That is a single 90-degree
# COUNTER-clockwise rotation:
#     u = (px - x0) / W * 44      (0..44 across the printed page, left to right)
#     v = (py - y0) / H * 60      (0..60 down the printed page)
#     x = v - 30                  (world inches, -30..30, red end negative)
#     z = u - 22                  (world inches, -22..22, matching svg_pt's y-up sense)
# Getting this backwards is nearly invisible -- these layouts are close to mirror-symmetric
# and a flipped board still "looks like a board" -- so the gates below check IDENTITY (does
# each objective land inside a terrain area, does the red DZ sit at negative x) rather than
# plausibility.
#
# WHAT IS A VECTOR HERE AND WHAT IS NOT
# Terrain AREAS, deployment zones, the territory line and the objective markers are all real
# vector paths and come out exact. Terrain FEATURES (the Battlefields: Armageddon ruins) are
# raster sprites; only their placements are recorded here, and turning those into hulls is
# the sprite-silhouette stage (P2), which is what bake_threat_maps.py needs for DENSE.
#
# TRAPS, all found the hard way on 2026-08-28:
#   - The 8 teal discs carrying the AB/CD/EF/GH letters are terrain-FEATURE LABELS, not
#     objectives. They are fill+stroke (white, 0.6) and ~16.4pt; objective badges are
#     fill-only and ~25.5pt. Mistaking them gives you 13 objectives per layout.
#   - Objective badges are drawn ~3.3in across. They are ICONS, not footprints -- only the
#     centre means anything. The area an objective belongs to comes from containment.
#   - The territory line is drawn TWICE on every page (identical path, two content streams).
#     Dedupe or you get 90 lines. It is frequently DIAGONAL: territory is not a board half.
#   - The paper-texture backdrop also has an aspect near 60/44 (1.38249). The board-rect
#     search has to be tight (0.005) or it locks onto the wrong image.

import collections
import hashlib
import json
import math
import os
import re
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from server.zones import CODE_ORDER            # noqa: E402

PDFDIR = r"C:/Users/User/Documents/TTS 40k Malaysia/11th Edition"
OUTDIR = os.path.join(ROOT, "docs", "reference", "gw")
OUT = os.path.join(OUTDIR, "gw_layouts.json")
MANIFEST = os.path.join(OUTDIR, "MANIFEST.json")

BOARD_W_IN = 44.0                # printed page is PORTRAIT: 44 across, 60 down
BOARD_H_IN = 60.0
ASPECT = BOARD_H_IN / BOARD_W_IN
BEZIER_STEPS = 8                 # flattening resolution for the organic area outlines

DISP = {"TAKE AND HOLD": "TH", "PURGE THE FOE": "PF", "DISRUPTION": "DI",
        "RECONNAISSANCE": "RE", "PRIORITY ASSETS": "PA"}

# Palette sampled off the artwork. Everything is matched with a tolerance because the PDF
# carries DeviceCMYK that MuPDF converts to RGB, so the values are not bit-exact between
# revisions -- but they are miles apart from each other, so 0.02 is safe.
C_AREA = (0.820, 0.826, 0.832)         # terrain area fill
C_RED = (0.618, 0.040, 0.056)          # attacker
C_BLUE = (0.000, 0.241, 0.408)         # defender
C_CENTRAL = (0.000, 0.452, 0.378)      # central objective disc (and the feature labels)
C_EXPANSION = (0.128, 0.571, 0.518)    # expansion objective diamond
C_DARK = (0.137, 0.122, 0.125)         # frame + territory line
C_EYE = (0.503, 0.510, 0.520)          # the Single/Separate Terrain Area eye markers
TOL = 0.02

# Eye markers, per the Layouts Key on p8: an OPEN eye means "Single Terrain Area" and a
# CROSSED-OUT eye means "Separate Terrain Areas". They sit on the join between two drawn areas
# and say whether those two count as ONE terrain area or two -- which is a rules fact, not
# decoration: it decides what is one obscuring region, and therefore what a model is wholly
# within. 220 of them across the 45 layouts, 104 single / 116 separate.
# Telling the two apart: the OPEN eye is drawn with a grey STROKED arc (type "s", width ~1.04)
# for the eyelid; the CROSSED one has no stroked path and instead carries two extra grey FILLS
# (the slash). Size is NOT a usable discriminator -- the two badges are 15.9pt and 15.3pt.
EYE_CLUSTER_PT = 12.0            # points: parts of one icon sit within this of each other
EYE_REACH = 2.5                  # in: how far the 2nd-nearest area may be before we warn

# Objective badge bbox side, points. WIDE on purpose: the expansion diamond is drawn at 28.7pt
# on 41 layouts but 21.7pt on PF-PF-A/PF-PF-C, and a 23pt floor silently dropped four real
# objectives. Every fill-only path in these three colours IS a badge -- the discriminator is
# colour + "no stroke" (the AB/CD/EF/GH feature labels carry a white 0.6 stroke), not size.
BADGE_PT = (19.0, 34.0)
LABEL_PT = (14.0, 19.0)          # AB/CD/EF/GH feature label disc, points
BADGE_REACH = 1.0                # in: how far GW may nudge an objective badge off its area
SYMMETRY_TOL = 6.0               # in: max residual against the 180-deg mirror before we warn


def near(a, b, tol=TOL):
    return a is not None and max(abs(p - q) for p, q in zip(a, b)) < tol


def newest_companion():
    best = None
    for fn in os.listdir(PDFDIR):
        low = fn.lower()
        if not low.endswith(".pdf") or "event_companion" not in low:
            continue
        if any(w in low for w in ("teams", "doubles", "dominatus")):
            continue           # those carry their own layouts; snapshotbot uses the main 45
        p = os.path.join(PDFDIR, fn)
        m = os.path.getmtime(p)
        if best is None or m > best[0]:
            best = (m, p)
    if best is None:
        raise SystemExit("no Warhammer Event Companion PDF under %s" % PDFDIR)
    return best[1]


def board_rect(page):
    # The 1in grid mat. Tight aspect window -- the paper texture is 1.38249 and would match
    # a loose one.
    best = None
    for info in page.get_image_info(xrefs=True):
        r = fitz.Rect(info["bbox"])
        if r.width < 150 or r.height < 200:
            continue
        if abs(r.height / r.width - ASPECT) > 0.005:
            continue
        a = r.width * r.height
        if best is None or a > best[0]:
            best = (a, r)
    return best[1] if best else None


def key_of(page):
    codes = []
    for blk in page.get_text("dict")["blocks"]:
        for line in blk.get("lines", []):
            for sp in line["spans"]:
                t = sp["text"].strip().upper()
                if t in DISP and sp["bbox"][1] < 200:
                    codes.append((sp["bbox"][0], DISP[t]))       # left-to-right = A then B
    m = re.search(r"LAYOUT ([ABC])", page.get_text())
    if len(codes) != 2 or not m:
        return None
    pair = sorted((c for _, c in codes), key=CODE_ORDER.index)
    return "%s-%s-%s" % (pair[0], pair[1], m.group(1))


class Board:
    def __init__(self, rect):
        self.r = rect

    def pt(self, p):
        u = (p.x - self.r.x0) / self.r.width * BOARD_W_IN
        v = (p.y - self.r.y0) / self.r.height * BOARD_H_IN
        return (round(v - 30.0, 3), round(u - 22.0, 3))          # (x, z) world inches

    def inside(self, xz, slack=0.25):
        return abs(xz[0]) <= 30.0 + slack and abs(xz[1]) <= 22.0 + slack


def cubic(p0, p1, p2, p3, n=BEZIER_STEPS):
    out = []
    for i in range(1, n + 1):
        t = i / n
        s = 1.0 - t
        out.append(fitz.Point(
            s * s * s * p0.x + 3 * s * s * t * p1.x + 3 * s * t * t * p2.x + t * t * t * p3.x,
            s * s * s * p0.y + 3 * s * s * t * p1.y + 3 * s * t * t * p2.y + t * t * t * p3.y))
    return out


def rings(items, board):
    # A drawing may hold several disjoint subpaths -- a terrain area with a notch, an
    # objective badge drawn as ring + glyph. PyMuPDF does not emit moveto markers, so a
    # subpath break is detected as a POSITION DISCONTINUITY between one item's end and the
    # next item's start. Concatenating them instead (the first cut of this did) stitches two
    # separate rings into one bogus polygon that wanders between them -- which is invisible in
    # a bbox check and only shows up as an objective mysteriously landing outside its own
    # terrain area.
    subs, cur, last = [], [], None
    for it in items:
        if it[0] == "l":
            seg = [it[1], it[2]]
        elif it[0] == "c":
            seg = [it[1]] + cubic(it[1], it[2], it[3], it[4])
        elif it[0] == "qu":
            q = it[1]
            seg = [q.ul, q.ur, q.lr, q.ll, q.ul]
        elif it[0] == "re":
            r = it[1]
            seg = [r.tl, r.tr, r.br, r.bl, r.tl]
        else:
            continue
        if last is not None and math.hypot(seg[0].x - last.x, seg[0].y - last.y) > 0.05:
            subs.append(cur)
            cur = []
        cur += seg
        last = seg[-1]
    subs.append(cur)

    out = []
    for sub in subs:
        poly = []
        for p in sub:
            w = board.pt(p)
            if not poly or w != poly[-1]:
                poly.append(list(w))
        if len(poly) > 1 and poly[0] == poly[-1]:
            poly.pop()
        if len(poly) >= 3:
            out.append(poly)
    return out


def flatten(items, board):
    # The outer ring: the largest subpath by area.
    rs = rings(items, board)
    return max(rs, key=poly_area) if rs else []


def poly_area(poly):
    a = 0.0
    for i in range(len(poly)):
        x1, z1 = poly[i - 1]
        x2, z2 = poly[i]
        a += x1 * z2 - x2 * z1
    return abs(a) / 2.0


def centroid(poly):
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


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


def seg_dist(p, a, b):
    dx, dz = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dz == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / (dx * dx + dz * dz)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dz))


def poly_dist(poly, x, z):
    # 0 when inside; otherwise the distance to the nearest EDGE. Nearest-VERTEX is not a
    # substitute -- on these densely-sampled outlines a point 0.08in outside an edge can be
    # 0.70in from the closest vertex, which reads as a miss when it is a seam.
    if in_poly(poly, x, z):
        return 0.0
    return min(seg_dist((x, z), poly[i - 1], poly[i]) for i in range(len(poly)))


def _bbox(poly):
    xs = [p[0] for p in poly]
    zs = [p[1] for p in poly]
    return (min(xs), min(zs), max(xs), max(zs))


def merge_groups(polys, tol=0.15):
    # Touching footprints are ONE terrain area in 11e -- GW routinely draws a single area as
    # two abutting mirror halves. Union-find on edge proximity, same idea as
    # bake_threat_maps.merge_touching (MERGE_TOL 0.10 there, read off the gap distribution).
    # Bbox rejection first: these outlines carry ~220 points each, and the naive all-pairs
    # form spends minutes per page comparing polygons that are 20in apart.
    n = len(polys)
    boxes = [_bbox(p) for p in polys]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            a, b = boxes[i], boxes[j]
            if a[0] - b[2] > tol or b[0] - a[2] > tol or a[1] - b[3] > tol or b[1] - a[3] > tol:
                continue
            if any(poly_dist(polys[j], x, z) <= tol for x, z in polys[i]):
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def bbox_side(rect):
    return max(rect.width, rect.height)


def eye_markers(page, br, board, areas):
    """The Single/Separate Terrain Area markers, each tied to the two areas it sits between."""
    parts = []
    for g in page.get_drawings():
        r = g["rect"]
        if not r.intersects(br):
            continue
        if not (near(g.get("fill"), C_EYE) or near(g.get("color"), C_EYE)):
            continue
        parts.append((((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0), g["type"]))
    clusters = []
    for c, t in parts:
        for cl in clusters:
            if abs(cl[0][0] - c[0]) < EYE_CLUSTER_PT and abs(cl[0][1] - c[1]) < EYE_CLUSTER_PT:
                cl[1].append(t)
                break
        else:
            clusters.append((c, [t]))

    out = []
    for c, kinds in clusters:
        x, z = board.pt(fitz.Point(*c))
        ranked = sorted((poly_dist(a, x, z), i) for i, a in enumerate(areas))
        joins = [i for _, i in ranked[:2]]
        out.append({"at": [x, z], "kind": "single" if "s" in kinds else "separate",
                    "areas": sorted(joins), "reach": round(ranked[1][0], 2) if len(ranked) > 1
                    else None})
    return out


def group_by_markers(areas, markers):
    """GW-annotation-driven grouping. NOT WIRED IN YET -- see the P5 note in docs/TASKS.md.

    This is the right idea and the wrong implementation. GW's markers ARE the authority on
    whether two drawn footprints are one terrain area, and the proximity merge that is still
    in use gets it wrong in both directions: it joins every pair within 0.15in (TH-TH-A has 7
    such joins and GW marks only 3 of them "Single"), and it never joins DI-PA-A's pairs that
    sit 2in APART and ARE marked Single.
    What is unsolved is ASSOCIATION: which two areas does a marker refer to? Nearest-two is not
    it. On PF-PA-C a "single" marker sits 2.62in from one area and 4.08in from the next, merges
    them, and drops a central and a home objective into the same group; on TH-RE-A one sits
    INSIDE area 3 and 2.88in from area 10. 217 of 220 markers pair cleanly, so the rule needs
    to be "the two areas whose shared boundary the marker lies on", not "the two closest".
    Until that is solved this would be a REGRESSION on grouping, so merge_groups still runs.
    """
    parent = list(range(len(areas)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for m in markers:
        if m["kind"] != "single" or len(m["areas"]) < 2:
            continue
        a, b = find(m["areas"][0]), find(m["areas"][1])
        if a != b:
            parent[a] = b
    groups = {}
    for i in range(len(areas)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def dz_axis(red, blue):
    # Which world axis separates the deployment zones. GW deploys along the printed page's
    # long edge on 30 layouts and its short edge on 15; after the CCW rotation those become
    # an x-split (red LEFT) and a z-split (red BOTTOM) respectively -- and that is exactly
    # snapshotbot's existing "SVG red is always the left/bottom side" convention, so no
    # 180-degree normalisation is needed. Red is first in GW's artwork on all 45.
    if not red or not blue:
        return None
    rc, bc = centroid(red), centroid(blue)
    return "x" if abs(rc[0] - bc[0]) >= abs(rc[1] - bc[1]) else "z"


def extract_page(page, problems):
    key = key_of(page)
    br = board_rect(page)
    if key is None or br is None:
        return None, None
    board = Board(br)
    areas, objs, zones = [], [], {"red": None, "blue": None}
    territory = None

    for g in page.get_drawings():
        r = g["rect"]
        if not r.intersects(br):
            continue
        f, s, w = g.get("fill"), g.get("color"), (g.get("width") or 0)

        if near(f, C_AREA) and s is not None:
            poly = flatten(g["items"], board)
            if len(poly) >= 3:
                areas.append(poly)
            continue

        for side, col in (("red", C_RED), ("blue", C_BLUE)):
            if not near(f, col):
                continue
            poly = flatten(g["items"], board)
            if len(poly) < 3:
                continue
            if BADGE_PT[0] <= bbox_side(r) <= BADGE_PT[1]:
                objs.append({"kind": "home", "owner": side, "at": list(centroid(poly))})
            elif zones[side] is None or poly_area(poly) > poly_area(zones[side]):
                zones[side] = poly                       # DZ = the largest fill of its colour

        # Central discs and the AB/CD/EF/GH feature labels share a fill colour; the labels
        # carry a white stroke and are ~16pt, the objective is fill-only and ~25pt.
        if near(f, C_CENTRAL) and s is None and BADGE_PT[0] <= bbox_side(r) <= BADGE_PT[1]:
            objs.append({"kind": "central", "owner": None,
                         "at": list(centroid(flatten(g["items"], board)))})
        if near(f, C_EXPANSION) and s is None and BADGE_PT[0] <= bbox_side(r) <= BADGE_PT[1]:
            objs.append({"kind": "expansion", "owner": None,
                         "at": list(centroid(flatten(g["items"], board)))})

        # The territory line spans the board, but along EITHER page axis: 30 layouts deploy
        # along the page's long edge and 15 along the short one, so a width-only test finds
        # barely half of them.
        if near(s, C_DARK) and 1.0 < w < 1.5 and len(g["items"]) == 1 and g["items"][0][0] == "l":
            a, b = g["items"][0][1], g["items"][0][2]
            if max(abs(b.x - a.x) / br.width, abs(b.y - a.y) / br.height) > 0.8:
                territory = [list(board.pt(a)), list(board.pt(b))]

    pieces = []
    for info in page.get_image_info(xrefs=True):
        r = fitz.Rect(info["bbox"])
        if not r.intersects(br) or r.width < 3 or bbox_side(r) > 200:
            continue                                     # skips the grid mat and the paper
        pieces.append({"xref": info["xref"], "bbox": [list(board.pt(r.tl)), list(board.pt(r.br))],
                       "transform": [round(v, 4) for v in info["transform"]]})

    # de-dup: several vector groups and every sprite are emitted twice per page
    objs = list({(o["kind"], o["owner"], round(o["at"][0], 1), round(o["at"][1], 1)): o
                 for o in objs}.values())
    areas = list({tuple(map(tuple, p)): p for p in areas}.values())
    pieces = list({(p["xref"], tuple(p["transform"])): p for p in pieces}.values())

    # 11 layouts print NO territory line: where territory splits on the board centre, the
    # grid mat's own dotted crosshair already shows it and GW draws nothing extra. Fall back
    # to the centre line PERPENDICULAR to the deployment axis -- which is what the mat shows.
    axis = dz_axis(zones["red"], zones["blue"])
    if territory is None and axis:
        territory = [[0.0, -22.0], [0.0, 22.0]] if axis == "x" else [[-30.0, 0.0], [30.0, 0.0]]

    eyes = eye_markers(page, br, board, areas)
    lay = {"key": key, "page": page.number, "terrain_areas": areas, "eye_markers": eyes,
           "area_groups": merge_groups(areas), "objectives": objs,
           "red_poly": zones["red"], "blue_poly": zones["blue"], "territory_line": territory,
           "dz_axis": axis, "territory_line_printed": territory is not None and axis is not None
           and territory not in ([[0.0, -22.0], [0.0, 22.0]], [[-30.0, 0.0], [30.0, 0.0]]),
           "pieces": pieces}
    check(lay, problems)
    return key, lay


def check(lay, problems):
    k = lay["key"]

    def bad(msg):
        problems.append("%s: %s" % (k, msg))

    for m in lay["eye_markers"]:
        if m["reach"] is not None and m["reach"] > EYE_REACH:
            lay.setdefault("warnings", []).append(
                "%s eye marker at %s sits %.2f in from its 2nd-nearest area -- its pairing is "
                "unreliable, see the P5 note" % (m["kind"], [round(v, 1) for v in m["at"]],
                                                 m["reach"]))
    if len(lay["terrain_areas"]) != 16:
        bad("%d terrain areas, expected 16" % len(lay["terrain_areas"]))
    for side in ("red_poly", "blue_poly"):
        if not lay[side]:
            bad("no %s" % side)
    if lay["territory_line"] is None:
        bad("no territory line")
    if lay["red_poly"] and lay["blue_poly"]:
        # ORIENTATION GATE: red must be the NEGATIVE side of whichever axis separates the
        # zones -- left on an x-split, bottom on a z-split. A 180-degree error passes every
        # plausibility check on these near-symmetric boards but fails this outright.
        rc, bc = centroid(lay["red_poly"]), centroid(lay["blue_poly"])
        i = 0 if lay["dz_axis"] == "x" else 1
        if not rc[i] < 0 < bc[i]:
            bad("red/blue deployment zones are the wrong way round on the %s axis (red %.1f, "
                "blue %.1f)" % (lay["dz_axis"], rc[i], bc[i]))
    homes = [o for o in lay["objectives"] if o["kind"] == "home"]
    if len(homes) != 2 or {o["owner"] for o in homes} != {"red", "blue"}:
        bad("%d home objectives (%s)" % (len(homes), [o["owner"] for o in homes]))
    if not 4 <= len(lay["objectives"]) <= 8:
        bad("%d objectives" % len(lay["objectives"]))
    # IDENTITY GATE: in 11e an objective IS a terrain area, so every marker must resolve to
    # one -- and to its OWN one. This is what actually catches a bad transform: distance to
    # SOME objective stays small under a sign flip on these near-symmetric boards, but
    # landing on the RIGHT area does not.
    #
    # Matched against MERGED groups of touching footprints, not raw ones, for two reasons
    # both seen in the artwork: GW draws several areas as two abutting mirror halves with the
    # objective centred on the join (inside neither), and on PA-PA-C it nudges the badge ~0.5in
    # clear of the area so the generator art underneath stays visible. The badge is a LOCATOR;
    # the area is the objective. BADGE_REACH bounds how far a nudge may go -- it is not a
    # tolerance for a bad transform, which lands objectives many inches out.
    groups = lay["area_groups"]
    claimed = {}
    for o in lay["objectives"]:
        ds = [min(poly_dist(lay["terrain_areas"][i], o["at"][0], o["at"][1]) for i in g)
              for g in groups]
        d = min(ds)
        gi = ds.index(d)
        o["group_i"] = gi
        o["badge_offset"] = round(d, 2)
        if d > BADGE_REACH:
            bad("%s objective at %s is %.2f in from the nearest terrain area"
                % (o["kind"], [round(v, 2) for v in o["at"]], d))
        elif gi in claimed:
            bad("%s and %s objectives both resolve to terrain area group %d"
                % (claimed[gi], o["kind"], gi))
        else:
            claimed[gi] = o["kind"]

    # SYMMETRY CHECK (warning, never fatal). GW places objectives in 180-degree rotational
    # pairs -- a lone central sits near the origin and is its own counterpart. Measured over
    # all 45 layouts the residual is median 0.54in and at most 3.1in (badges get nudged onto
    # their own areas independently), so 6in separates artwork jitter from a MISSING objective
    # by a wide margin: 1 of 210 trips it.
    # It is a warning because when it fires the fault is usually in GW's artwork, not here --
    # v1.2 dropped one of DI-PA-C's two expansion objectives, and aborting the whole pipeline
    # over someone else's typo would be wrong. It must still be loud.
    for o in lay["objectives"]:
        x, z = o["at"]
        if math.hypot(x, z) < 4.5:
            continue
        peer = [math.hypot(-x - p["at"][0], -z - p["at"][1])
                for p in lay["objectives"] if p is not o and p["kind"] == o["kind"]]
        o["pair_residual"] = round(min(peer), 2) if peer else None
        if not peer or min(peer) > SYMMETRY_TOL:
            lay.setdefault("warnings", []).append(
                "%s objective at %s has no 180-degree counterpart -- GW artwork may be missing "
                "one" % (o["kind"], [round(v, 2) for v in o["at"]]))


def restore_orphans(out):
    """Put back objectives GW's artwork dropped, at the 180-degree mirror of the survivor.

    v1.2 lost one of DI-PA-C's two expansion objectives. The proof that it is an ERROR and not a
    design choice is a rules invariant, checked by sibling_gate() below: objective counts follow
    the disposition PAIRING, not the A/B/C variant, and DI-PA-C is the only layout of the 45
    that differs from its own two siblings. Restored objectives are tagged so they are never
    mistaken for something GW printed, and the symmetry warning keeps firing.
    """
    n = 0
    for lay in out.values():
        for o in list(lay["objectives"]):
            if o.get("pair_residual") is not None or math.hypot(*o["at"]) <= 4.5:
                continue
            lay["objectives"].append(
                {"kind": o["kind"], "owner": o.get("owner"), "restored": True,
                 "at": [round(-o["at"][0], 2), round(-o["at"][1], 2)],
                 "group_i": None, "badge_offset": None, "pair_residual": 0.0})
            n += 1
    return n


def sibling_gate(out, problems):
    """Objective counts are set by the disposition PAIRING, so A/B/C must agree.

    This is the check that identified DI-PA-C's missing objective in the first place, and it is
    worth keeping permanently: it turns "GW changed something" into "GW made a typo" without
    anyone having to eyeball 45 pages.
    """
    fam = collections.defaultdict(dict)
    for key, lay in out.items():
        pair, variant = key.rsplit("-", 1)
        fam[pair][variant] = tuple(sorted(collections.Counter(
            o["kind"] for o in lay["objectives"]).items()))
    for pair, variants in sorted(fam.items()):
        if len(set(variants.values())) > 1:
            problems.append("%s: variants disagree on objective counts %s -- a pairing's A/B/C "
                            "must match" % (pair, {k: dict(v) for k, v in sorted(variants.items())}))


def main():
    pdf = newest_companion()
    doc = fitz.open(pdf)
    problems, out = [], {}
    pages = 0
    for page in doc:
        key, lay = extract_page(page, problems)
        if key is None:
            continue
        pages += 1
        if key in out:
            problems.append("%s: duplicate key (pages %d and %d)"
                            % (key, out[key]["page"], lay["page"]))
        out[key] = lay

    restored = restore_orphans(out)
    sibling_gate(out, problems)

    nobj = collections.Counter(len(v["objectives"]) for v in out.values())
    diag = collections.Counter(
        "diagonal" if abs(v["territory_line"][0][0] - v["territory_line"][1][0]) > 0.5 else "flat"
        for v in out.values() if v["territory_line"])

    print("source               : %s" % os.path.basename(pdf))
    print("layout pages found   : %d   unique keys: %d" % (pages, len(out)))
    print("terrain areas        : %d  (expect %d)"
          % (sum(len(v["terrain_areas"]) for v in out.values()), 16 * len(out)))
    print("objectives per layout: %s   (restored %d GW omission%s)"
          % (dict(sorted(nobj.items())), restored, "" if restored == 1 else "s"))
    print("territory lines      : %s" % dict(diag))
    warns = [(k, w) for k, v in sorted(out.items()) for w in (v.get("warnings") or [])]
    eyes = collections.Counter(m["kind"] for v in out.values() for m in v["eye_markers"])
    print("eye markers          : %d single + %d separate = %d  (area groups: %d)"
          % (eyes["single"], eyes["separate"], sum(eyes.values()),
             sum(len(v["area_groups"]) for v in out.values())))
    print("sprite placements    : %d over %d distinct images"
          % (sum(len(v["pieces"]) for v in out.values()),
             len({p["xref"] for v in out.values() for p in v["pieces"]})))

    if warns:
        print("\nWARNINGS (%d)  -- anomalies in GW's artwork, not extraction failures:"
              % len(warns))
        for k, w in warns:
            print("   %s: %s" % (k, w))

    if len(out) != 45 or pages != 45:
        problems.append("expected 45 layout pages, got %d (%d unique)" % (pages, len(out)))
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems[:30]:
            print("   " + p)
        print("\nABORTED -- nothing written.")
        return 1

    os.makedirs(OUTDIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    with open(pdf, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump({"note": "GW Warhammer Event Companion is the source of layout geometry; "
                           "rapidingress is retired. Re-run extract_gw_layouts.py when a new "
                           "companion ships with a dataslate.",
                   "source_pdf": os.path.basename(pdf), "md5": md5,
                   "layouts": len(out), "board_rect_pt": "constant on all 45 pages",
                   "transform": "x = v - 30, z = u - 22  (90deg CCW from GW's portrait page)"},
                  f, indent=2)
        f.write("\n")
    print("\nOK -- %s, %d bytes" % (OUT, os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
