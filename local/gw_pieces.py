# P2 -- terrain FEATURE silhouettes and their DENSE/LIGHT class, from the Event Companion.
#
# Why this exists: features carry rule weight. A DENSE feature is a solid structure that blocks
# line of sight unconditionally (bake_threat_maps.py leans on exactly this); a LIGHT one does
# not. GW ships the features as raster sprites, not vectors, so the class and the footprint both
# have to be recovered from the artwork.
#
# THREE KINDS OF SPRITE SIT ON A LAYOUT BOARD, and telling them apart is the whole problem:
#   base plate   the rusty footprint art UNDER a terrain area -- one per area, 16 per layout.
#                NOT terrain in its own right; the area polygon from P1 already describes it.
#   dense        the teal ruins.
#   light        the gold railings/barricades.
# Dense vs everything else is trivial and robust: median HUE is 170.5-171.0 for teal and
# 32-43 for gold, a 128-degree gap, so no threshold needs tuning.
#
# Base plate vs LIGHT feature is the hard one -- both are gold, and they are NOT separable by
# size. Taking "the 16 largest gold sprites per layout" reproduces rapidingress's 720/719/630
# counts exactly, which looks like success and is not: the smallest base is 19.6 sq in against a
# largest light of 19.3 sq in (ratio 1.01), and spot-checking the individual placements showed 8
# of 1350 assigned to the wrong class, with the per-layout COUNTS still landing right because a
# base and a light swapped places. Bbox IoU against the area polygons is no better (266 of 720
# bases score below 0.55, 42 of 630 lights score above it).
# What actually separates them is IDENTITY. The same handful of sprites are reused across all
# 45 layouts, so the class is a property of the SPRITE, not of the placement: 62 base-plate
# xrefs, 8 light, 26 dense. The size split is used ONCE to seed a per-xref majority vote, and
# after that the library decides. Only 2 xrefs are ambiguous under the seed (5658 base 96 /
# light 4, 5610 base 4 / light 86) and the vote resolves both.
#
# THE GATE THAT MATTERS: every layout must come out with exactly 16 base plates. That is not a
# tuned number -- it is GW's own published footprint table on p7 (4x 6"x4", 2x 10"x2.5",
# 4x 6"x2", 4x 7"x11.5", 2x 8"x11.5" = 16). If a future companion changes the terrain set this
# fails loudly instead of silently reclassifying half the board.

import collections
import colorsys
import math

import fitz

HUE_TEAL = 90.0            # anything above is dense; the two clusters sit at ~171 and ~37
ALPHA_ON = 128
HULL_TOL = 0.012           # unit-square units; ~1.2% of the sprite's long side
MAX_TRACE = 220            # downsample the alpha mask to this on the long side before tracing


def _rgba(doc, xref):
    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace is None:
        return None
    if pix.colorspace.n > 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    sm = doc.extract_image(xref).get("smask", 0)
    if sm:
        try:
            pix = fitz.Pixmap(pix, fitz.Pixmap(doc, sm))
        except Exception:
            pass
    return pix


def median_hue(doc, xref, cache={}):
    if xref in cache:
        return cache[xref]
    pix = _rgba(doc, xref)
    hs = []
    if pix is not None:
        s, n, w, h = pix.samples, pix.n, pix.width, pix.height
        step = max(1, (w * h) // 3000)
        for i in range(0, w * h, step):
            o = i * n
            if n == 4 and s[o + 3] < ALPHA_ON:
                continue
            hh, ss, vv = colorsys.rgb_to_hsv(s[o] / 255.0, s[o + 1] / 255.0, s[o + 2] / 255.0)
            if ss > 0.25 and vv > 0.15:
                hs.append(hh * 360.0)
    hs.sort()
    cache[xref] = hs[len(hs) // 2] if hs else -1.0
    return cache[xref]


def _mask(doc, xref):
    # Binary alpha mask, downsampled, as (rows, cols, grid) with grid[r][c] in {0,1}.
    pix = _rgba(doc, xref)
    if pix is None:
        return 0, 0, None
    w, h, n, s = pix.width, pix.height, pix.n, pix.samples
    step = max(1, max(w, h) // MAX_TRACE)
    cols, rows = w // step, h // step
    if cols < 3 or rows < 3:
        return 0, 0, None
    grid = [[0] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            o = ((r * step) * w + c * step) * n
            grid[r][c] = 1 if (n != 4 or s[o + 3] >= ALPHA_ON) else 0
    return rows, cols, grid


def _largest_blob(rows, cols, grid):
    seen = [[False] * cols for _ in range(rows)]
    best = []
    for r0 in range(rows):
        for c0 in range(cols):
            if grid[r0][c0] != 1 or seen[r0][c0]:
                continue
            stack, blob = [(r0, c0)], []
            seen[r0][c0] = True
            while stack:
                r, c = stack.pop()
                blob.append((r, c))
                for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < rows and 0 <= nc < cols and not seen[nr][nc] \
                            and grid[nr][nc] == 1:
                        seen[nr][nc] = True
                        stack.append((nr, nc))
            if len(blob) > len(best):
                best = blob
    return best


def _trace(rows, cols, blob):
    # Moore-neighbour boundary trace of one blob, clockwise from its top-left-most cell.
    inb = set(blob)
    start = min(blob)
    nbr = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))
    contour = [start]
    cur, bdir = start, 6
    for _ in range(8 * len(blob) + 8):
        found = False
        for k in range(8):
            d = (bdir + k) % 8
            nr, nc = cur[0] + nbr[d][0], cur[1] + nbr[d][1]
            if (nr, nc) in inb:
                cur = (nr, nc)
                bdir = (d + 5) % 8
                contour.append(cur)
                found = True
                break
        if not found or (len(contour) > 2 and cur == start):
            break
    return contour


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


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


def simplify_ring(poly, tol):
    """Douglas-Peucker on a closed ring, in whatever units the ring is already in.

    Gate on MAX DEVIATION, never on area: a densely-sampled outline has consecutive points
    ~0.03in apart, so a collinearity test collapses a 250-point ruin to 5 points while the
    enclosed area barely moves. That exact trap is recorded in rebuild_layouts_meta.py.
    """
    ring = list(poly) + [poly[0]]
    out = _dp(ring, tol)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out if len(out) >= 3 else list(poly)


def ring_deviation(poly, simplified):
    """Worst distance from an original vertex to the simplified outline."""
    worst = 0.0
    n = len(simplified)
    for p in poly:
        worst = max(worst, min(_seg_dist(p, simplified[i - 1], simplified[i]) for i in range(n)))
    return worst


def hull(doc, xref, tol=HULL_TOL):
    # Silhouette of the sprite in UNIT space: u across, v down, both 0..1, matching the
    # convention of the placement matrix returned by get_image_info.
    rows, cols, grid = _mask(doc, xref)
    if grid is None:
        return None
    blob = _largest_blob(rows, cols, grid)
    if len(blob) < 12:
        return None
    contour = _trace(rows, cols, blob)
    pts = [((c + 0.5) / cols, (r + 0.5) / rows) for r, c in contour]
    out = _dp(pts, tol)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return [[round(u, 4), round(v, 4)] for u, v in out] if len(out) >= 3 else None


def build_library(doc, layouts):
    """{xref: {"cls": dense|light|base, "hue": float, "hull": [[u,v]...] or None}}"""
    seed = collections.defaultdict(collections.Counter)
    for lay in layouts.values():
        gold = []
        for p in lay["pieces"]:
            if median_hue(doc, p["xref"]) > HUE_TEAL:
                continue
            (x0, z0), (x1, z1) = p["bbox"]
            gold.append((abs(x1 - x0) * abs(z1 - z0), p["xref"]))
        gold.sort(reverse=True)
        for i, (_, x) in enumerate(gold):
            seed[x]["base" if i < 16 else "light"] += 1

    lib = {}
    for lay in layouts.values():
        for p in lay["pieces"]:
            x = p["xref"]
            if x in lib:
                continue
            h = median_hue(doc, x)
            if h > HUE_TEAL:
                cls = "dense"
            else:
                c = seed.get(x)
                cls = "base" if (c and c["base"] >= c["light"]) else "light"
            lib[x] = {"cls": cls, "hue": round(h, 1),
                      "hull": hull(doc, x) if cls != "base" else None}
    return lib


def place(hull_uv, transform):
    """Unit-space hull -> page points, via get_image_info's placement matrix."""
    a, b, c, d, e, f = transform
    return [(a * u + c * v + e, b * u + d * v + f) for u, v in hull_uv]


def placed_features(lay, lib, board):
    """[(cls, [[x, z], ...]), ...] -- every terrain FEATURE of a layout in world inches.

    Both the SVG emitter and the threat-map bake go through here, deliberately: the polygon the
    viewer paints as a dense ruin is then literally the same polygon that blocks line of sight
    in the bake. Let those two drift and the board stops explaining the overlay drawn on it.
    """
    import fitz

    out = []
    for p in lay["pieces"]:
        m = lib[p["xref"]]
        if m["cls"] == "base" or not m["hull"]:
            continue
        pts = [board.pt(fitz.Point(px, py)) for px, py in place(m["hull"], p["transform"])]
        out.append((m["cls"], [list(q) for q in pts]))
    return out
