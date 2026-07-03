# Mesh geometry pipeline: measures the true base disc and renders a top-down
# silhouette from each model's sculpt file (OBJ on Steam's CDN). Runs in background
# threads server-side — never on the table. Results cache forever by mesh key
# (yellowscribe models keep the same ugc id across every list that uses the sculpt).
import io
import math
import threading
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

from server import db

PPI = 40                     # silhouette raster px per inch (reduced for huge meshes)
MAX_DIM_PX = 800             # cap either raster dimension; ppi drops to fit
PAD = 0.15                   # inches of canvas padding around the sculpt
MAX_OBJ_BYTES = 30 * 1024 * 1024
# Junk guard only. Some legit meshes are authored huge and shrunk by the instance
# scale at spawn (Rogal Dorn ships ~40 mesh-units wide) — the viewer rescales via
# the per-model scale, so big spans are fine; only absurd ones are corrupt exports.
MAX_SPAN_IN = 100

ALLOWED_HOSTS = (".akamaihd.net", ".steamusercontent.com")


def _fetch(url):
    host = urllib.parse.urlparse(url).hostname or ""
    if not host.endswith(ALLOWED_HOSTS):
        raise ValueError("mesh host not allowed: " + host)
    req = urllib.request.Request(url, headers={"User-Agent": "snapshotbot-geom"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(MAX_OBJ_BYTES + 1)
    if len(data) > MAX_OBJ_BYTES:
        raise ValueError("obj too large")
    return data.decode("utf-8", errors="replace")


def _parse_obj(text):
    verts, faces = [], []
    for line in text.splitlines():
        if line.startswith("v "):
            p = line.split()
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif line.startswith("f "):
            idx = [int(t.split("/")[0]) for t in line.split()[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            for k in range(1, len(idx) - 1):  # fan-triangulate n-gons
                faces.append((idx[0], idx[k], idx[k + 1]))
    if not verts or not faces:
        raise ValueError("empty obj")
    return verts, faces


def _base_size(verts):
    # Bottom slice of the parent mesh = the base. For yellowscribe models the parent
    # IS a flat base disc, so the slice is the whole mesh; single-mesh models get
    # their bottom 10% (min 0.15") — the base rim.
    ys = [v[1] for v in verts]
    y0, y1 = min(ys), max(ys)
    cut = y0 + max(0.15, (y1 - y0) * 0.10)
    xs = [v[0] for v in verts if v[1] <= cut]
    zs = [v[2] for v in verts if v[1] <= cut]
    w, h = max(xs) - min(xs), max(zs) - min(zs)
    if w <= 0 or h <= 0:
        raise ValueError("degenerate base slice")
    if abs(w - h) <= 0.08 * max(w, h):
        return {"d": round((w + h) / 2, 2)}
    return {"wh": [round(w, 2), round(h, 2)]}


def _triangles(spec, fetch=_fetch):
    # Parent mesh (base disc or whole sculpt) + optional child sculpt transformed
    # into the parent's frame, projected to the XZ plane.
    pv, pf = _parse_obj(fetch(spec["mesh"]))
    tris = [tuple((pv[i][0], pv[i][2]) for i in t) for t in pf]
    base = _base_size(pv)
    if spec.get("child_mesh"):
        cv, cf = _parse_obj(fetch(spec["child_mesh"]))
        ry = math.radians(-(spec.get("child_rot") or 0))
        cs = spec.get("child_scale") or 1
        cx, cz = spec.get("child_x") or 0, spec.get("child_z") or 0
        cosr, sinr = math.cos(ry), math.sin(ry)
        for a, b, c in cf:
            tris.append(tuple(
                (v[0] * cs * cosr - v[2] * cs * sinr + cx,
                 v[0] * cs * sinr + v[2] * cs * cosr + cz)
                for v in (cv[a], cv[b], cv[c])))
    return base, tris


def _rasterize(tris):
    # White-on-transparent mask; viewer tints per team. Image +y (down) = world -z,
    # matching the board canvas orientation (north up).
    xs = [p[0] for t in tris for p in t]
    zs = [p[1] for t in tris for p in t]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    if maxx - minx > MAX_SPAN_IN or maxz - minz > MAX_SPAN_IN:
        raise ValueError("sculpt footprint too large")
    span = max(maxx - minx, maxz - minz) + 2 * PAD
    ppi = min(PPI, MAX_DIM_PX / span)
    w = max(1, int((maxx - minx + 2 * PAD) * ppi))
    h = max(1, int((maxz - minz + 2 * PAD) * ppi))
    mask = Image.new("L", (w, h), 0)
    drw = ImageDraw.Draw(mask)

    def px(p):
        return ((p[0] - minx + PAD) * ppi, (maxz + PAD - p[1]) * ppi)

    for t in tris:
        drw.polygon([px(p) for p in t], fill=255)
    out = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    out.putalpha(mask)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    # (ox, oy) = pixel of the model's origin, the anchor getPosition() reports.
    meta = {"ppi": round(ppi, 2), "ox": round((0 - minx + PAD) * ppi, 1),
            "oy": round((maxz + PAD - 0) * ppi, 1)}
    return buf.getvalue(), meta


def compute(spec, fetch=_fetch):
    # Shared by the Railway worker and the local pre-crunch tool — same math,
    # different mesh source.
    base, tris = _triangles(spec, fetch)
    png, meta = _rasterize(tris)
    return base, png, meta


def _process(key):
    spec = db.geom_claim(key)
    if spec is None:
        return
    try:
        base, png, meta = compute(spec)
        db.geom_finish(key, base, png, meta)
        print(f"[geom] done key={key} name={spec.get('name')} "
              f"base={base} png={len(png) // 1024}KB", flush=True)
    except Exception as e:  # noqa: BLE001 — any failure just marks the row
        db.geom_fail(key, str(e)[:500])
        print(f"[geom] FAILED key={key} name={spec.get('name')}: {e}", flush=True)


def enqueue(key, name, spec):
    if db.geom_upsert(key, name, spec):
        threading.Thread(target=_process, args=(key,), daemon=True).start()


def resume_pending():
    for key in db.geom_stuck():
        threading.Thread(target=_process, args=(key,), daemon=True).start()
