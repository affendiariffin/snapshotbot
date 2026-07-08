# Local pre-crunch: extract every model from ForceOrg (or any TTS save/list JSON),
# compute base + silhouette ON THIS PC, and upload finished rows to sb_mesh_geom.
# Railway then serves everything from cache — its own worker only fires on true
# misses (kitbashes that never passed through ForceOrg).
#
# Mesh sources, in order: TTS's own Mods/Models cache -> local/mesh_cache/ ->
# Steam CDN (saved into local/mesh_cache for the next run). Fully resumable:
# keys already 'done' in the DB are skipped.
#
# Usage (DATABASE_URL = the PUBLIC Railway DSN):
#   python local/precrunch.py                  # full ForceOrg library
#   python local/precrunch.py --limit 25       # trial run
#   python local/precrunch.py --source "path/to/list.json"
import argparse
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import db, meshgeom  # noqa: E402

FORCEORG = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Mods/Workshop/3137407072.json")
TTS_CACHE = os.path.expanduser("~/Documents/My Games/Tabletop Simulator/Mods/Models")
LOCAL_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh_cache")
LONGSTR = re.compile(r"\[\[(.*?)\]\]", re.S)


def walk(o):
    yield o
    for c in o.get("ContainedObjects") or []:
        yield from walk(c)
    for c in (o.get("States") or {}).values():
        yield from walk(c)


def child_specs(children, ids, depth=1):
    # Mirror of the token's childSpecs walk (≤6 descendants, ≤3 deep): the WHOLE
    # child tree is the assembly — flight-stand vehicles carry the hull as a
    # second child (Raider = disc + stand + hull-with-grandchild), and keying
    # parent-child[1] collided different vehicles onto one stand-only silhouette.
    out = []
    for ch in children or []:
        if len(ids) >= 6 or depth > 3:
            break
        curl = (ch.get("CustomMesh") or {}).get("MeshURL")
        cid = re.search(r"/ugc/(\d+)/", curl) if curl else None
        t = ch.get("Transform")
        if cid and t:
            ids.append(cid.group(1))
            node = {"mesh": curl, "rot": t.get("rotY", 0), "x": t.get("posX", 0),
                    "z": t.get("posZ", 0), "scale": t.get("scaleX", 1)}
            kids = child_specs(ch.get("ChildObjects"), ids, depth + 1)
            if kids:
                node["children"] = kids
            out.append(node)
    return out


def spec_of(o):
    cm = o.get("CustomMesh") or {}
    if not o.get("Name", "").startswith(("Custom_Model", "Figurine")) or not cm.get("MeshURL"):
        return None, None
    spec = {"mesh": cm["MeshURL"], "name": (o.get("Nickname") or "")[:100]}
    pid = re.search(r"/ugc/(\d+)/", cm["MeshURL"])
    if not pid:
        return None, None
    ids = []
    kids = child_specs(o.get("ChildObjects"), ids)
    if kids:
        spec["children"] = kids
    key = "-".join([pid.group(1)] + ids)
    return key, spec


def extract_specs(path):
    d = json.load(open(path, encoding="utf-8"))
    specs = {}

    def take(obj):
        for o in walk(obj):
            key, spec = spec_of(o)
            if key and key not in specs:
                specs[key] = spec

    for top in d["ObjectStates"]:
        take(top)
        if top.get("Name") == "Custom_Tile" and len(top.get("LuaScript") or "") > 100000:
            for m in LONGSTR.finditer(top["LuaScript"]):
                try:
                    take(json.loads(m.group(1)))
                except json.JSONDecodeError:
                    pass
    return specs


def cache_name(url):
    return re.sub(r"[^A-Za-z0-9]", "", url) + ".obj"


downloaded = 0


def fetch(url):
    global downloaded
    fn = cache_name(url)
    for folder in (TTS_CACHE, LOCAL_CACHE):
        p = os.path.join(folder, fn)
        if os.path.exists(p):
            return open(p, encoding="utf-8", errors="replace").read()
    req = urllib.request.Request(url, headers={"User-Agent": "snapshotbot-precrunch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(meshgeom.MAX_OBJ_BYTES + 1)
    if len(data) > meshgeom.MAX_OBJ_BYTES:
        raise ValueError("obj too large")
    os.makedirs(LOCAL_CACHE, exist_ok=True)
    with open(os.path.join(LOCAL_CACHE, fn), "wb") as f:
        f.write(data)
    downloaded += 1
    time.sleep(0.2)  # be polite to the CDN
    return data.decode("utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=FORCEORG)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true", help="recompute even if already done")
    ap.add_argument("--keys", default="", help="comma-separated keys to restrict to")
    args = ap.parse_args()

    specs = extract_specs(args.source)
    if args.keys:
        want = set(args.keys.split(","))
        specs = {k: s for k, s in specs.items() if k in want}
    done = set() if args.force else db.geom_done_keys()
    todo = [(k, s) for k, s in specs.items() if k not in done]
    print(f"{len(specs)} unique models in source, {len(done)} already done, {len(todo)} to crunch")
    if args.limit:
        todo = todo[: args.limit]

    ok = fail = 0
    t0 = time.time()
    for i, (key, spec) in enumerate(todo, 1):
        try:
            base, png, meta = meshgeom.compute(spec, fetch)
            db.geom_put_done(key, spec["name"], spec, base, png, meta, overwrite=args.force)
            ok += 1
        except Exception as e:  # noqa: BLE001 — log and keep crunching
            fail += 1
            print(f"  FAIL {key} {spec['name']!r}: {e}")
        if i % 25 == 0 or i == len(todo):
            rate = i / max(time.time() - t0, 1)
            print(f"  {i}/{len(todo)} ok={ok} fail={fail} dl={downloaded} "
                  f"({rate:.1f}/s, ~{int((len(todo) - i) / max(rate, 0.01) / 60)}min left)", flush=True)
    print(f"finished: ok={ok} fail={fail} downloaded={downloaded}")


if __name__ == "__main__":
    main()
