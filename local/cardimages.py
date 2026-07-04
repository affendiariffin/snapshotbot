# Extract mission/secondary/twist card FACE IMAGES from the local LCT mod and
# upload to sb_card_images — the replay shows the actual card instead of linking
# to third-party rules sites. Each LCT card is its own 1x1 atlas: no cropping.
# Sources: TTS's Mods/Images cache first, then the CDN. Resumable (skips done keys).
#
# Usage:  DATABASE_URL=<public DSN> python local/cardimages.py [--force]
import argparse
import io
import json
import os
import re
import sys
import urllib.request

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import db  # noqa: E402

MOD = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Mods/Workshop/3710681747.json")
TTS_IMAGES = os.path.expanduser("~/Documents/My Games/Tabletop Simulator/Mods/Images")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "card_cache")

MAP_CARD_RE = re.compile(r"\bvs\b.*-")   # "Dis vs Dis 1 - Crucible of Battle - BTTF"
MAX_H = 700
JPEG_Q = 87


def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def walk(o):
    yield o
    for c in o.get("ContainedObjects") or []:
        yield from walk(c)
    for c in (o.get("States") or {}).values():
        yield from walk(c)


def collect():
    d = json.load(open(MOD, encoding="utf-8"))
    out = {}
    for o in walk({"ContainedObjects": d["ObjectStates"]}):
        n = o.get("Nickname") or ""
        if o.get("Name") != "CardCustom" or not n or MAP_CARD_RE.search(n):
            continue
        for spec in (o.get("CustomDeck") or {}).values():
            if spec.get("NumWidth") == 1 and spec.get("NumHeight") == 1 and spec.get("FaceURL"):
                out.setdefault(norm(n), (n, spec["FaceURL"]))
    return out


def fetch(url):
    fn = re.sub(r"[^A-Za-z0-9]", "", url)
    for folder, exts in ((TTS_IMAGES, (".png", ".jpg", ".jpeg")), (CACHE, ("",))):
        for ext in exts:
            p = os.path.join(folder, fn + ext)
            if os.path.exists(p):
                return open(p, "rb").read()
    req = urllib.request.Request(url, headers={"User-Agent": "snapshotbot-cards"})
    data = urllib.request.urlopen(req, timeout=60).read()
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, fn), "wb") as f:
        f.write(data)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cards = collect()
    done = set() if args.force else db.card_keys()
    todo = {k: v for k, v in cards.items() if k not in done}
    print(f"{len(cards)} cards in mod, {len(done)} already stored, {len(todo)} to do")

    ok = fail = 0
    for key, (name, url) in sorted(todo.items()):
        try:
            img = Image.open(io.BytesIO(fetch(url)))
            img = img.convert("RGB")
            if img.height > MAX_H:
                img = img.resize((round(img.width * MAX_H / img.height), MAX_H))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=JPEG_Q, optimize=True)
            db.card_put(key, name, buf.getvalue())
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {name!r}: {e}")
    print(f"finished: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
