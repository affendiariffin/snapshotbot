# Bake the deployment THREAT MAPS -- server/static/layouts/heat/<KEY>-<side>.png.
#
# KEEPER, same standing as rebuild_layouts_meta.py: the geometry comes from the rapidingress
# archive under docs/reference/rapidingress/, so this re-runs whenever that archive refreshes
# (check the three ETags against its MANIFEST.json first).
#
# WHAT IT COMPUTES. One 8-bit grid per layout per side. Cell value = the fraction of the OTHER
# side's FORWARD DEPLOYMENT LINE that can draw line of sight to that cell. The question it answers,
# in Fendi's words (2026-08-11): "which part of my own deployment zone can I stage in -- especially
# with a large model -- without being shot on the enemy's turn 1". The viewer paints it under the
# models during deployment frames only.
#
# THE FIRING SET IS THE FORWARD LINE, NOT THE ZONE INTERIOR (Fendi, 2026-08-11). The most
# aggressive legal deployment is right on the line, which is the case worth planning against, and
# rear positions are redundant. MEASURED over 6 layouts before adopting it:
#   - the line alone sees 98.5% of every cell the whole zone interior can see (96.9-99.7%);
#   - enemy-zone exposure reads 5.43x higher off the line, because interior positions that see
#     nothing were dividing the signal away. That dilution -- not the display -- was the reason an
#     earlier revision had to crank the render gamma to make anything visible.
# It is also ~4x cheaper: ~55 firing positions instead of ~250.
#
# WHY DEPLOYMENT ONLY. It is a tractability boundary, not a UI preference. Any-point-to-any-point
# visibility on this grid is 10,560^2 ~ 111M point pairs per layout (~7h for 45, ~14MB of raw
# visibility bits each -- unshippable). A deployment line is FIXED, so the firing set collapses to
# ~55 x 10,560 ~ 0.6M rays, i.e. seconds.
#
# MODEL SIZE NEEDS NO HANDLING, and this is the one thing to not "improve" later. During deployment
# a model must be placed WHOLLY WITHIN its zone (infiltrators excepted, out of scope), so:
#   - shooter side: sample points sit ON the line, NOT inset by a base radius. A model must fit
#     wholly inside its zone, but its forward-most PART can touch the line and 40k draws LOS from
#     any part -- so the line itself is the occupiable frontier.
#   - target side: the map stays point-exposure. A dark blob wide enough to fit the Knight is a
#     safe spot for the Knight -- the reader's eye does the union. An earlier draft baked per-cell
#     firing-position bitsets and OR-pooled them into four radius bands; Fendi cut it as 4x the
#     artefacts to pre-compute something you can already see.
#
# LOS RULESET (11e core 13.07-13.11), shinebot's model with ONE deliberate divergence:
#   - a terrain AREA holding >=1 LIGHT/DENSE feature is OBSCURING;
#   - toe-in is BASE OVERLAP, not centre-inside, so areas are tested buffered by TOE;
#   - DENSE feature hulls are SOLID at ground level and block UNCONDITIONALLY -- including to a
#     target standing inside one. They are structures, not regions; no toe-in or target-in relief;
#   - ground level, 2D. No elevation, no Plunging Fire, no >3in gaps. Same simplification shinebot
#     ships, and the same one rapidingress ships.
#   - SEE_IN (Fendi's ruling for THIS map, 2026-08-11): a ray sees INTO a non-own obscuring area
#     and stops at its exit edge, so a cell inside an area is visible from outside it. NOTE this is
#     the OPPOSITE of shinebot's live setting (SEE_INTO_AREAS=false, perimeter-stop). Deliberate,
#     not drift: under perimeter-stop every terrain area reads a flat 0% and the map can only grade
#     open ground. Both variants were rendered for TH-RE-C and he ruled on the pair.
#
# Zones come from layouts_meta.json's red_poly/blue_poly, NOT from rapidingress's own
# deploymentZones: those are labelled attacker/defender, and mapping that onto our red/blue would
# be a guess. red_poly/blue_poly are already world-space and already agree with what the viewer
# paints, which is the only agreement that matters.
#
# PNG ORIENTATION -- the one thing to get right, and the one thing that looks fine when wrong.
#   row 0 = z MAX (top of the board), column 0 = x MIN. Standard image convention, so the viewer's
#   drawImage over the board rect needs no flip, exactly like the layout SVG it sits on.
#   world -> pixel:  col = (x + 30) / CELL      row = (22 - z) / CELL
# The in-memory grid is built z-ASCENDING (cz runs -22 -> +22), so the write flips it. An early
# revision of this script did not, and shipped every map upside down -- while the eyeball report
# looked perfect, because render() applies its own flip. That is why gate_readback exists: it
# re-opens the WRITTEN FILE and checks it against a fresh from-geometry recomputation, which is the
# only check that spans bake -> file -> world coords. Do not replace it with an in-memory
# assertion. gate_shadow is NOT the orientation check and must never be described as one -- it is a
# DIAGNOSTIC that writes to `warnings` and cannot fail a bake, because measurement showed it both
# misses 21 of 135 single-axis mirrors AND flags correct grids.

import argparse
import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from server.zones import CODE_ORDER      # noqa: E402

LAYDIR = os.path.join(ROOT, "server", "static", "layouts")
META = os.path.join(LAYDIR, "layouts_meta.json")
HEATDIR = os.path.join(LAYDIR, "heat")
RIDIR = os.path.join(ROOT, "docs", "reference", "rapidingress")
RIJSON = os.path.join(RIDIR, "ri_layouts_11e.json")
REPORT = os.path.join(ROOT, "docs", "threat_bake_report.md")

CELL = 0.5               # inches per grid cell
FIRE = 1.0               # spacing of sampled firing positions ALONG THE FORWARD LINE
TOE = 0.63               # 32mm base radius -- replay.html's default 1.26in diameter
HX, HZ = 30.0, 22.0
GAMMA = 0.40             # alpha = ALPHA * exposure^GAMMA -- the viewer must use the same curve
ALPHA = 1.56             # was 0.78; Fendi 2026-08-11 "halve the transparency, it's hard to see"
ALPHA_CAP = 0.95         # never fully opaque -- the terrain art has to stay readable underneath
MERGE_TOL = 0.10         # footprints closer than this are ONE terrain piece (see merge_touching)
RULESET = "seein-v1"     # bump when the LOS model changes; recorded in the manifest


def ri_pt(p):
    return (p["x"] - 30.0, p["y"] - 22.0)


def poly_of(pts):
    g = Polygon(pts)
    return g if g.is_valid else g.buffer(0)


def edges_of(geom):
    out = []
    parts = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
    for g in parts:
        if g.is_empty:
            continue
        for ring in [g.exterior] + list(g.interiors):
            c = np.asarray(ring.coords[:-1], float)
            if len(c) >= 2:
                out.append(np.stack([c, np.roll(c, -1, axis=0)], axis=1))
    return np.concatenate(out) if out else np.zeros((0, 2, 2))


def crosses(origin, targets, e):
    # Segment/segment intersection of one origin against every target, vectorised over edges.
    p = np.asarray(origin, float)
    r = targets - p
    q, s = e[:, 0, :], e[:, 1, :] - e[:, 0, :]
    rxs = r[:, None, 0] * s[None, :, 1] - r[:, None, 1] * s[None, :, 0]
    qp = q[None, :, :] - p[None, None, :]
    qpr = qp[:, :, 0] * r[:, None, 1] - qp[:, :, 1] * r[:, None, 0]
    qps = qp[:, :, 0] * s[None, :, 1] - qp[:, :, 1] * s[None, :, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        t, u = qps / rxs, qpr / rxs
    hit = (np.abs(rxs) > 1e-12) & (t > 1e-6) & (t < 1 - 1e-6) & (u > 1e-6) & (u < 1 - 1e-6)
    return hit.any(axis=1)


def merge_touching(polys):
    # Where footprints are combined on the table they are ONE terrain piece (Fendi, 2026-08-11),
    # but rapidingress still gives them separate areaIds -- TH-TH-B's central ruin is two plates
    # that literally overlap. Left split, each half blocks LOS to the other and the piece renders
    # with a hard diagonal seam down the middle, one side lit and one dark.
    #
    # MERGE_TOL is read off the data, not guessed: over all 45 layouts the 3,837 inter-area
    # distances cluster at <0.10in (116 pairs, i.e. touching or overlapping) and then jump -- only
    # 5 pairs in the whole 0.10-1.00in band, 3,660 beyond 2in. So 0.10 captures exactly the
    # combined pieces. Gaps are closed with a mitred buffer out-and-back so a hairline 0.02in seam
    # cannot be threaded by a ray.
    out, used = [], set()
    for i, g in enumerate(polys):
        if i in used:
            continue
        comp, frontier = [i], [i]
        used.add(i)
        while frontier:
            k = frontier.pop()
            for j, h in enumerate(polys):
                if j not in used and polys[k].distance(h) < MERGE_TOL:
                    used.add(j)
                    comp.append(j)
                    frontier.append(j)
        b = MERGE_TOL / 2 + 0.005
        g2 = unary_union([polys[c].buffer(b, join_style=2) for c in comp]).buffer(
            -b, join_style=2)
        out.append(g2.simplify(0.05))
    return out


def build_terrain(src):
    # PLATE pieces (base=true) are the terrain AREA footprints; DENSE features are the solid walls.
    # losPoints is rapidingress's own LOS hull where present -- prefer it over the detailed outline.
    areas, dense = {}, []
    for t in src["terrain"]:
        g = poly_of([ri_pt(p) for p in (t.get("losPoints") or t["points"])])
        if g.is_empty:
            continue
        if t.get("base"):
            areas.setdefault(t["areaId"], []).append(g)
        elif t.get("category") == "DENSE":
            dense.append(g.simplify(0.1))
    by_id = [unary_union(v).simplify(0.1) for v in areas.values()]
    return merge_touching(by_id), merge_touching(dense)


def grid_points():
    cx = np.arange(-HX + CELL / 2, HX, CELL)
    cz = np.arange(-HZ + CELL / 2, HZ, CELL)
    pts = np.array([(x, z) for z in cz for x in cx])
    return cx, cz, pts


def perimeter_segment(a, b, tol=0.5):
    # A segment RUNS ALONG the board perimeter only if both ends sit on the SAME edge line. Testing
    # each endpoint independently is wrong twice over: a full-width forward line has both ends on
    # the side edges and would be dropped entirely, and a forward line legitimately ENDS at the
    # board edge.
    if abs(a[0] - b[0]) < tol and abs(abs(a[0]) - HX) < tol:
        return True
    return abs(a[1] - b[1]) < tol and abs(abs(a[1]) - HZ) < tol


def forward_edge(zone):
    # The zone boundary MINUS the parts running along the board perimeter -- i.e. the line facing
    # no-man's land. Zone polygons are inset ~0.15in from the true board edge, hence the tolerance.
    coords = list(zone.exterior.coords)
    return [(a, b) for a, b in zip(coords, coords[1:]) if not perimeter_segment(a, b)]


def firing_positions(zone, dense, areas):
    # THE FORWARD LINE ONLY -- see the header for the measured justification (98.5% coverage,
    # 5.43x dilution removed). Points sit ON the line, not inset by a base radius.
    #
    # THE LINE STOPS JUST BEFORE TERRAIN (Fendi, 2026-08-11). Where a zone boundary happens to run
    # across a terrain area, a firing position there is TOED INTO that area -- and 13.10 then makes
    # the WHOLE area transparent for it, so it shoots clean through a 12in ruin and lights up open
    # ground on the far side that nothing in the zone can actually see. He spotted exactly that on
    # TH-RE-C. Rather than model the toe-in exclusion more finely, drop those positions: the line
    # ends just before the terrain. Cheap, unambiguous, and it removes the artefact at its source
    # instead of arguing about how much of a ruin a toed-in model may see through.
    out = []
    for a, b in forward_edge(zone):
        seg = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        if seg < 1e-9:
            continue
        for t in np.arange(0.0, seg, FIRE):
            p = Point(a[0] + (b[0] - a[0]) * t / seg, a[1] + (b[1] - a[1]) * t / seg)
            if any(d.contains(p) for d in dense):               # nothing occupies a solid wall
                continue
            if any(g.distance(p) < TOE for g in areas):         # standing in/at terrain: skip
                continue
            out.append((p.x, p.y))
    return out


def exposure(zone, areas, dense, pts, see_in=True):
    a_edges = [edges_of(g) for g in areas]
    a_toe = [g.buffer(TOE) for g in areas]
    d_edges = [edges_of(g) for g in dense]
    fps = firing_positions(zone, dense, areas)
    if not fps:
        return None, []
    # Target-side exclusion is STRICT CONTAINMENT, not the toe buffer. Same confusion Fendi called
    # out at the zone end, mirrored at the target end: a buffered test treats open ground up to
    # TOE outside a ruin as being "in" it, so the ruin stops blocking and the ground beside it
    # lights up through the ruin. Clearing it took both ends -- stopping the line before terrain
    # fixed the first marked region, this fixed the second.
    in_toe = np.stack([np.array([g.contains(Point(*c)) for c in pts]) for g in areas]) \
        if areas else np.zeros((0, len(pts)), bool)

    seen = np.zeros(len(pts), np.int32)
    for f in fps:
        pf = Point(f)
        blocked = np.zeros(len(pts), bool)
        for i, e in enumerate(a_edges):
            if a_toe[i].contains(pf):
                continue                                   # unreachable: the line stops before
            hit = crosses(f, pts, e)
            # see-in: the ray stops at the area's EXIT edge, so a target inside is still visible.
            blocked |= hit & ~in_toe[i] if see_in else hit
        for i, e in enumerate(d_edges):
            # DENSE IS SOLID -- it blocks unconditionally, including TO cells inside it (Fendi,
            # 2026-08-11: "green terrain pieces physically block line of sight"). An earlier
            # revision excused a target sitting inside a dense hull, by analogy with the obscuring
            # rule, and every dense piece lit up as a result. The analogy is wrong: an obscuring
            # AREA is a region a model stands in, a dense FEATURE is a solid structure, and you
            # cannot see through a wall to something behind it just because it is inside.
            blocked |= crosses(f, pts, e)
        seen += ~blocked
    return seen / len(fps), fps


def gate_merge(key, areas, dense, problems):
    # Merging must leave no two distinct pieces still touching -- otherwise a seam survives and
    # each half goes on blocking LOS to the other, which is the artefact this exists to remove.
    for name, polys in (("area", areas), ("dense", dense)):
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                if polys[i].distance(polys[j]) < MERGE_TOL:
                    problems.append("%s: %ss %d and %d are still %.3fin apart after merging"
                                    % (key, name, i, j, polys[i].distance(polys[j])))
                    return


def gate_zone(key, side, zone, fps, dense, areas, problems):
    # The forward line must be a PROPER subset of the zone boundary: non-empty (we did not classify
    # the whole thing away) and materially shorter than the perimeter (we did drop the board-edge
    # runs). Both failure directions have already happened once.
    fwd = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in forward_edge(zone))
    per = zone.exterior.length
    if fwd < 5.0:
        problems.append("%s/%s: forward line is %.1fin -- edge classification dropped it"
                        % (key, side, fwd))
        return
    if fwd > per - 10.0:
        problems.append("%s/%s: forward line %.1fin vs perimeter %.1fin -- board-edge runs were "
                        "not removed" % (key, side, fwd, per))
        return
    for f in fps:
        p = Point(f)
        if zone.exterior.distance(p) > 0.01:
            problems.append("%s/%s: firing position %r is not on the zone boundary"
                            % (key, side, f))
            return
        if any(d.contains(p) for d in dense):
            problems.append("%s/%s: firing position %r inside a DENSE hull" % (key, side, f))
            return
        if any(g.distance(p) < TOE for g in areas):
            problems.append("%s/%s: firing position %r is toed into an obscuring area -- it would "
                            "see through the whole of it" % (key, side, f))
            return


def mask_own_zone(expo, pts, zone):
    # The firing side's OWN deployment zone is masked to zero (Fendi, 2026-08-11): "from blue's
    # perspective the lines need only project towards him -- projections within red's own
    # deployment are irrelevant". Nothing of blue's is ever standing in red's zone during
    # deployment, and it was the brightest region of every map (own-zone median 0.31, ~1.0 along
    # the line itself), so it dominated the ramp while carrying no information for the reader.
    # Masked = "this map has nothing to say here", which renders the same as safe -- acceptable,
    # because the red map is only ever shown while hovering a BLUE model.
    out = expo.copy()
    out[np.array([zone.contains(Point(*p)) for p in pts])] = 0.0
    return out


def to_image(expo, cx, cz):
    # z-ascending grid -> image rows (row 0 = z max). See PNG ORIENTATION in the header.
    return (expo.reshape(len(cz), len(cx))[::-1] * 255).round().astype(np.uint8)


def gate_readback(key, side, path, zone, areas, dense, see_in, problems, n=24):
    # THE orientation gate. Re-open the written PNG and check it against a fresh recomputation
    # straight from geometry at random world points, through the documented world->pixel mapping.
    # Any mirror, transpose or half-cell offset in the write path fails here; nothing else in this
    # script spans bake -> file -> world coords.
    px = np.asarray(Image.open(path), np.float32) / 255.0
    cx, cz, _ = grid_points()
    # Probe CELL CENTRES, not arbitrary points: exposure has hard shadow edges, so a probe offset
    # inside a cell disagrees with the stored centre value by up to ~0.1 on its own. Sampling off
    # centre made this gate fire on every correct bake -- the fix is the probe, not the tolerance.
    rng = np.random.default_rng(abs(hash((key, side))) % (2 ** 32))
    idx = np.stack([rng.integers(0, len(cx), n), rng.integers(0, len(cz), n)], axis=1)
    probe = np.stack([cx[idx[:, 0]], cz[idx[:, 1]]], axis=1)
    truth, _ = exposure(zone, areas, dense, probe, see_in)
    if truth is None:
        return
    # Apply the same mask, so this also validates that the masked region landed in the right
    # place -- a flipped grid would put the zero block on the wrong side of the board and fail here.
    truth = mask_own_zone(truth, probe, zone)
    worst = 0.0
    for (x, z), want in zip(probe, truth):
        col = int(round((x + HX - CELL / 2) / CELL))
        row = int(round((HZ - CELL / 2 - z) / CELL))
        worst = max(worst, abs(px[row, col] - want))
    if worst > 1.5 / 255.0:                                # 1 quantisation step of slack
        problems.append("%s/%s: written PNG disagrees with a from-geometry recompute by %.3f -- "
                        "orientation or indexing is wrong" % (key, side, worst))
    return worst


def gate_shadow(key, side, expo, cx, cz, fps, dense, warnings):
    # DIAGNOSTIC, NOT A GATE -- it writes to `warnings` and cannot fail a bake. Measured, it both
    # misses real corruptions (21 of 135 single-axis mirrors) and flags correct grids: TH-TH-A is a
    # mirror-symmetric layout whose two sides score 2/11 and 3/11 on identical geometry, i.e. one
    # hull either side of a threshold. A check that produces both false negatives and false
    # positives is a smell detector, and dressing it up as a gate would mean either trusting it or
    # tuning its threshold until the bake goes green. gate_readback is the orientation check.
    # Kept because it is nearly free and a sudden jump across many layouts would mean something.
    g = expo.reshape(len(cz), len(cx))

    def sample(x, z):
        ix = int(round((x + HX - CELL / 2) / CELL))
        iz = int(round((z + HZ - CELL / 2) / CELL))
        if 0 <= ix < len(cx) and 0 <= iz < len(cz):
            return g[iz, ix]
        return None

    # Direction of illumination is the FIRING LINE's centroid, not the zone's: once the firing set
    # became the forward line, a zone-centroid direction pointed the test the wrong way for hulls
    # beside the line and the false-flag rate tripled.
    zcx = float(np.mean([f[0] for f in fps]))
    zcz = float(np.mean([f[1] for f in fps]))
    tested = flipped = 0
    for d in dense:
        c = d.centroid
        vx, vz = c.x - zcx, c.y - zcz
        n = (vx * vx + vz * vz) ** 0.5
        if n < 1e-6:
            continue
        vx, vz = vx / n, vz / n
        reach = max(d.bounds[2] - d.bounds[0], d.bounds[3] - d.bounds[1]) / 2 + 1.0
        near = sample(c.x - vx * reach, c.y - vz * reach)
        far = sample(c.x + vx * reach, c.y + vz * reach)
        # "Too close to call" must scale with the quantisation step (1/len(fps)), not be a fixed
        # 0.02: once the line lost its toed-in positions the step grew to 1/41 = 0.024, the skip
        # stopped triggering, and advisory warnings jumped 1 -> 9 with no change in the maps.
        if near is None or far is None or abs(near - far) < max(0.02, 1.5 / len(fps)):
            continue
        tested += 1
        if far > near:
            flipped += 1
    if tested and flipped > tested * 0.25:
        warnings.append("%s/%s: %d/%d solid walls are brighter behind than in front"
                        % (key, side, flipped, tested))
    return tested, flipped


def gate_own_zone(key, side, expo, pts, zone, other, problems):
    # You can see most of your own zone; the far half of theirs is where cover lives.
    own = np.array([zone.contains(Point(*p)) for p in pts])
    far = np.array([other.contains(Point(*p)) for p in pts])
    if own.any() and far.any() and expo[own].mean() <= expo[far].mean():
        problems.append("%s/%s: own-zone mean %.3f <= enemy-zone mean %.3f -- sides look swapped"
                        % (key, side, expo[own].mean(), expo[far].mean()))
    # p90 over the ENEMY zone is the number the viewer's ramp has to render: it is where the
    # feature's answer lives, and it sits around 0.14 -- two orders below the grid's nominal range.
    return ((expo[own].mean() if own.any() else 0.0),
            (expo[far].mean() if far.any() else 0.0),
            (float(np.percentile(expo[far], 90)) if far.any() else 0.0))


def render(expo, cx, cz, base_png, side):
    # Preview only -- consumes to_image() so it cannot disagree with the shipped PNG's orientation.
    # GAMMA is the viewer's ramp. Even off the forward line, exposure INSIDE a deployment zone tops
    # out around 0.14 at p90 -- not a dilution artefact any more, just what 20in of no-man's land
    # through terrain does -- so a near-linear curve still renders the answer as nothing.
    lo, hi = ((0x7a, 0x1e, 0x24), (0xff, 0x6b, 0x6b)) if side == "red" \
        else ((0x18, 0x28, 0x55), (0x7a, 0xb8, 0xff))
    base = Image.open(base_png).convert("RGB")
    w, h = base.size
    gi = np.asarray(Image.fromarray(to_image(expo, cx, cz)).resize((w, h), Image.BILINEAR),
                    np.float32) / 255.0
    col = np.array(lo, np.float32) + (np.array(hi, np.float32) - np.array(lo, np.float32)) \
        * gi[..., None]
    a = np.minimum(ALPHA_CAP, ALPHA * gi ** GAMMA)[..., None]
    out = np.asarray(base, np.float32) * (1 - a) + col * a
    return Image.fromarray(out.clip(0, 255).astype(np.uint8))


def main():
    global CELL, FIRE
    ap = argparse.ArgumentParser()
    ap.add_argument("--layouts", help="comma-separated keys; default all 45")
    ap.add_argument("--perimeter-stop", action="store_true",
                    help="shinebot's ruling instead of see-in (rays stop at the ENTRY edge)")
    ap.add_argument("--report", type=int, default=3, help="layouts to composite into the report")
    ap.add_argument("--dry-run", action="store_true", help="gates + report, write no PNGs")
    ap.add_argument("--cell", type=float, help="grid inches (default %.2f) -- coarsen for sweeps"
                    % CELL)
    ap.add_argument("--fire", type=float, help="firing-position spacing (default %.2f)" % FIRE)
    args = ap.parse_args()
    see_in = not args.perimeter_stop
    if args.cell:
        CELL = args.cell
    if args.fire:
        FIRE = args.fire

    with open(META, encoding="utf-8") as f:
        meta = json.load(f)
    with open(RIJSON, encoding="utf-8") as f:
        ri = {}
        for x in json.load(f):
            codes = sorted(x["matchup"], key=CODE_ORDER.index)
            ri["%s-%s-%s" % (codes[0], codes[1], x["variant"])] = x

    keys = args.layouts.split(",") if args.layouts else sorted(meta)
    os.makedirs(HEATDIR, exist_ok=True)
    cx, cz, pts = grid_points()
    problems, warnings, rows, shots = [], [], [], []

    for key in keys:
        src = ri.get(key)
        if not src:
            problems.append("%s: absent from rapidingress" % key)
            continue
        areas, dense = build_terrain(src)
        gate_merge(key, areas, dense, problems)
        zones = {"red": poly_of(meta[key]["red_poly"]), "blue": poly_of(meta[key]["blue_poly"])}
        for side in ("red", "blue"):
            # `side` names the FIRING zone: <KEY>-red.png is what red's guns cover.
            expo, fps = exposure(zones[side], areas, dense, pts, see_in)
            if expo is None:
                problems.append("%s/%s: no firing positions" % (key, side))
                continue
            gate_zone(key, side, zones[side], fps, dense, areas, problems)
            tested, flipped = gate_shadow(key, side, expo, cx, cz, fps, dense, warnings)
            own, far, p90 = gate_own_zone(key, side, expo, pts, zones[side],
                                          zones["blue" if side == "red" else "red"], problems)
            # Gates above run on the RAW field -- gate_own_zone compares own vs enemy means to
            # catch swapped sides, and would be meaningless once the own zone reads zero.
            vis = mask_own_zone(expo, pts, zones[side])
            nomans = float(vis[~np.array([zones["red"].contains(Point(*p))
                                          or zones["blue"].contains(Point(*p))
                                          for p in pts])].mean())
            readback = None
            if not args.dry_run:
                path = os.path.join(HEATDIR, "%s-%s.png" % (key, side))
                Image.fromarray(to_image(vis, cx, cz)).save(path)
                readback = gate_readback(key, side, path, zones[side], areas, dense, see_in,
                                         problems)
            rows.append((key, side, len(fps), nomans, own, far, p90, tested, flipped,
                         readback, bool(meta[key].get("stale_dataslate"))))
            base_png = os.path.join(LAYDIR, "png", "%s.png" % key)
            if len(shots) < args.report * 2 and os.path.exists(base_png):
                shots.append((key, side, render(vis, cx, cz, base_png, side)))
            print("%-9s %-4s fps=%-4d nomans=%.3f own=%.3f enemy=%.3f p90=%.3f shadow=%d/%d%s"
                  % (key, side, len(fps), nomans, own, far, p90, flipped, tested,
                     "" if readback is None else "  readback=%.4f" % readback))

    if not args.dry_run:
        digests = {}
        for name in ("ri_terrain-data-11e.js", "ri_deployment-data.js", "ri_measurements-11e.js"):
            with open(os.path.join(RIDIR, name), "rb") as f:
                digests[name] = hashlib.md5(f.read()).hexdigest()
        with open(os.path.join(HEATDIR, "MANIFEST.json"), "w", encoding="utf-8") as f:
            json.dump({"cell_in": CELL, "firing_spacing_in": FIRE, "toe_in": TOE,
                       "ruleset": RULESET if see_in else "perimeter-v1",
                       "see_in": see_in, "gamma": GAMMA, "alpha": ALPHA,
                       "alpha_cap": ALPHA_CAP, "grid": [len(cx), len(cz)],
                       "firing_set": "forward line, stopping TOE short of any obscuring area",
                       "masked": "the firing side's own deployment zone reads 0 -- not applicable,"
                                 " not safe",
                       "layouts": sorted({r[0] for r in rows}), "source_md5": digests}, f, indent=2)

    write_report(rows, shots, problems, warnings, see_in)
    if warnings:
        print("\nwarnings (%d, advisory -- see gate_shadow):" % len(warnings))
        for w in warnings:
            print("  " + w)
    if problems:
        print("\nGATE FAILURES (%d):" % len(problems))
        for p in problems:
            print("  " + p)
        return 1
    print("\nall gates clean")
    return 0


def write_report(rows, shots, problems, warnings, see_in):
    shotdir = os.path.join(ROOT, "docs", "threat_bake")
    os.makedirs(shotdir, exist_ok=True)
    lines = ["# Threat-map bake report", "",
             "Ruling: **%s**. Cell %.2fin, firing grid %.2fin, toe-in %.2fin."
             % ("see-in (13.10)" if see_in else "perimeter-stop", CELL, FIRE, TOE),
             "`<KEY>-<side>.png`: *side* names the FIRING zone -- `-red` is what red's guns cover,",
             "so it is read over BLUE's ground; red's own zone is masked out.",
             "PNG rows run z MAX -> z MIN (image convention); `readback` is the max disagreement",
             "between the written file and a from-geometry recompute, in exposure units.",
             "", "**`enemy p90` is the number that matters** -- the exposure the 90th-percentile",
             "cell of the enemy's deployment zone carries, i.e. how hot the hottest pockets of the",
             "ground they are staging on actually get. It is what the viewer's ramp has to render.",
             "", "The firing side's OWN zone is masked to zero in the PNG -- it carries no",
             "information for the reader (see mask_own_zone). `own zone` below is measured",
             "BEFORE masking, and only exists so the swapped-sides gate has something to test.",
             "", "| layout | firing side | positions | no-man's land | own zone (pre-mask) |"
             " enemy zone | enemy p90 | shadow flips | readback | stale |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for key, side, n, mean, own, far, p90, tested, flipped, readback, stale in rows:
        lines.append("| %s | %s | %d | %.3f | %.3f | %.3f | %.3f | %d/%d | %s | %s |"
                     % (key, side, n, mean, own, far, p90, flipped, tested,
                        "-" if readback is None else "%.4f" % readback, "yes" if stale else ""))
    lines += ["", "## Eyeball", ""]
    for key, side, img in shots:
        name = "%s-%s.png" % (key, side)
        img.save(os.path.join(shotdir, name))
        lines.append("**%s** -- exposure from the %s zone\n\n![%s](threat_bake/%s)\n"
                     % (key, side, name, name))
    if problems:
        lines += ["## Gate failures", ""] + ["- " + p for p in problems]
    if warnings:
        lines += ["", "## Warnings (advisory -- gate_shadow is a diagnostic, not a gate)",
                  ""] + ["- " + w for w in warnings]
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
