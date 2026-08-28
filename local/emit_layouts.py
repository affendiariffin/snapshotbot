# P3 -- write the 45 layout SVGs into server/static/layouts/ from the GW extraction.
#
# Offline, re-runnable, a KEEPER. Inputs: docs/reference/gw/gw_layouts.json (P1) and the sprite
# library built by gw_pieces (P2). Output: server/static/layouts/<KEY>.svg, overwriting the
# rapidingress-harvested files. Those are tracked in git, so a bad run is one checkout away.
#
# WHAT ACTUALLY READS THESE FILES (checked, not assumed):
#   - the viewer paints one as the board background (replay.html: bgImg.src)
#   - app.py inlines one into each offline download
#   - rebuild_layouts_meta.territory_line() reads <g id="territory-line-layer"> at BUILD time
# zones.py does NOT parse SVGs at runtime -- it reads layouts_meta.json, which already carries
# red_poly/blue_poly. So the SVG is a picture plus one build-time hook, and the RI-compatible
# layer names are kept for that hook and for anyone reading the file later.
#
# PALETTE IS INHERITED from the rapidingress files, deliberately (Fendi, 2026-08-28): the point
# of the swap is new geometry, not a new look, and the team-tinted silhouettes and the threat
# overlay were tuned against these values.
#   terrain area  #4a4a4a fill / #fff stroke      dense feature  #277d27 @0.55 / #2a7d2a
#   light feature #e0c050 @0.45 / #c9a227          zones  #ff4a4a / #4a9eff @0.14, NO border
#   territory line #555                            objective icons: see gw_svg
#
# THREE RENDERING RULES THAT ARE NOT COSMETIC, each learned by getting it wrong:
#   1. Paint ALL area fills, then ALL area strokes, then features. Per-polygon fill+stroke lets
#      an abutting area cover its neighbour's white border.
#   2. Close every ring explicitly (gw_svg.poly_points). MuPDF does not stroke a <polygon>'s
#      closing edge; Firefox does. The 7"x11.5" areas close over a 7.03in edge, so the border
#      vanishes in some tools and not others.
#   3. Use fill + fill-opacity, never rgba(). Equivalent, more portable, and MuPDF renders
#      rgba() as black.
#
# DI-PA-C's restored objective arrives already in gw_layouts.json -- the restore and the
# sibling gate that justifies it live in extract_gw_layouts.py, so the SVG, layouts_meta.json
# and anything else downstream all inherit ONE decision rather than each re-deriving it.

import json
import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import extract_gw_layouts as E                                      # noqa: E402
import gw_pieces as G                                              # noqa: E402
from gw_svg import eyes_layer, objectives_layer, poly_points, svg_xy   # noqa: E402

SRC = os.path.join(ROOT, "docs", "reference", "gw", "gw_layouts.json")
DEST = os.path.join(ROOT, "server", "static", "layouts")

AREA_FILL = "#4a4a4a"
AREA_LINE = "#fff"
FEATURE = {"dense": ("#277d27", "0.55", "#2a7d2a"), "light": ("#e0c050", "0.45", "#c9a227")}
ZONE = {"red": "#ff4a4a", "blue": "#4a9eff"}
ZONE_OPACITY = "0.14"
TERRITORY = "#555"
GRID = "#e0e0e0"
BG = "#12141a"

# Display simplification. The extraction keeps every traced vertex (198,450 of them across the
# 45 layouts) because that is the measurement record; the SVG is a picture and does not need
# them. 0.05in is a quarter of a rendered pixel at 20px/in, so it cannot be seen, and it pulls
# the files back under the rapidingress ones they replace -- which matters because app.py
# inlines a whole SVG into every offline download.
DISPLAY_TOL = 0.05


def svg_for(lay, lib, board, stats):
    areas = []
    for p in lay["terrain_areas"]:
        q = G.simplify_ring(p, DISPLAY_TOL)
        stats["before"] += len(p)
        stats["after"] += len(q)
        stats["dev"] = max(stats["dev"], G.ring_deviation(p, q))
        areas.append(q)

    def sprite(piece):
        pts = G.place(lib[piece["xref"]]["hull"], piece["transform"])
        return poly_points([board.pt(fitz.Point(px, py)) for px, py in pts])

    grid = "".join('<line x1="%d" y1="0" x2="%d" y2="880"></line>' % (i * 20, i * 20)
                   for i in range(61))
    grid += "".join('<line x1="0" y1="%d" x2="1200" y2="%d"></line>' % (j * 20, j * 20)
                    for j in range(45))

    zones = ""
    for side, poly in (("red", lay["red_poly"]), ("blue", lay["blue_poly"])):
        zones += ('<polygon points="%s" fill="%s" fill-opacity="%s" data-zone="%s"></polygon>'
                  % (poly_points(poly), ZONE[side], ZONE_OPACITY, side))

    fills = "".join('<polygon points="%s" fill="%s"></polygon>'
                    % (poly_points(p), AREA_FILL) for p in areas)
    lines = "".join('<polygon points="%s" fill="none" stroke="%s" stroke-width="2"></polygon>'
                    % (poly_points(p), AREA_LINE) for p in areas)
    feats = ""
    for cls in ("dense", "light"):
        fill, op, stroke = FEATURE[cls]
        for p in lay["pieces"]:
            if lib[p["xref"]]["cls"] != cls:
                continue
            feats += ('<polygon points="%s" fill="%s" fill-opacity="%s" stroke="%s" '
                      'stroke-width="1.5" data-terrain="%s"></polygon>'
                      % (sprite(p), fill, op, stroke, cls))

    (ax, az), (bx, bz) = lay["territory_line"]
    (x1, y1), (x2, y2) = svg_xy(ax, az), svg_xy(bx, bz)
    line = ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="3" '
            'stroke-dasharray="9 7"></line>' % (x1, y1, x2, y2, TERRITORY))

    return ('<svg width="1200" height="880" xmlns="http://www.w3.org/2000/svg" '
            'id="battlefield-svg" viewBox="0 0 1200 880" preserveAspectRatio="xMidYMid meet" '
            'data-layout="%s" data-source="GW Warhammer Event Companion">'
            '<rect width="1200" height="880" fill="%s"></rect>'
            '<g id="grid-layer" stroke="%s" stroke-width="1" opacity="0.12">%s</g>'
            '<g id="deployment-zones-layer">%s</g>'
            '<g id="terrain-layer">%s%s%s</g>'
            '<g id="territory-line-layer">%s</g>%s%s</svg>'
            % (lay["key"], BG, GRID, grid, zones, fills, lines, feats, line,
               eyes_layer(lay["eye_markers"]), objectives_layer(lay["objectives"])))


def main():
    with open(SRC, encoding="utf-8") as f:
        layouts = json.load(f)
    doc = fitz.open(E.newest_companion())
    lib = G.build_library(doc, layouts)

    problems, written = [], 0
    stats = {"before": 0, "after": 0, "dev": 0.0}
    for key, lay in sorted(layouts.items()):
        page = doc[lay["page"]]
        board = E.Board(E.board_rect(page))
        body = svg_for(lay, lib, board, stats)
        # Gate: rebuild_layouts_meta.territory_line() must be able to read what we wrote, and
        # the zone polygons must round-trip back to the world coords they came from.
        if 'id="territory-line-layer"' not in body or "<line" not in body:
            problems.append("%s: no territory line in the emitted SVG" % key)
        for side, poly in (("red", lay["red_poly"]), ("blue", lay["blue_poly"])):
            want = "%.1f,%.1f" % svg_xy(poly[0][0], poly[0][1])
            if want not in body:
                problems.append("%s: %s zone polygon did not round-trip" % (key, side))
        with open(os.path.join(DEST, key + ".svg"), "w", encoding="utf-8") as f:
            f.write(body)
        written += 1

    sizes = [os.path.getsize(os.path.join(DEST, k + ".svg")) for k in layouts]
    print("layouts written      : %d" % written)
    print("objectives           : %d  (%d restored upstream, tagged)"
          % (sum(len(v["objectives"]) for v in layouts.values()),
             sum(1 for v in layouts.values() for o in v["objectives"] if o.get("restored"))))
    print("svg size             : min %d  median %d  max %d bytes"
          % (min(sizes), sorted(sizes)[len(sizes) // 2], max(sizes)))
    print("area vertices        : %d -> %d  (max deviation %.4f in, tol %.2f)"
          % (stats["before"], stats["after"], stats["dev"], DISPLAY_TOL))
    if stats["dev"] > DISPLAY_TOL * 1.5:
        problems.append("display simplification moved an outline %.3f in" % stats["dev"])
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems[:20]:
            print("   " + p)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
