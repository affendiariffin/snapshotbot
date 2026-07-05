# Extract LCT status-marker token images (Battle Shocked, +1 to Rolls, Objective
# Secured, ...) from the local mod into server/static/markers/ + markers.json.
# Markers are flat Custom_Tokens with a CustomImage; the replay draws the real
# token art with a side-coloured border. Rerun after LCT updates.
import io
import json
import os
import re
import sys
import urllib.request

from PIL import Image

MOD = os.path.expanduser(
    "~/Documents/My Games/Tabletop Simulator/Mods/Workshop/3710681747.json")
TTS_IMAGES = os.path.expanduser("~/Documents/My Games/Tabletop Simulator/Mods/Images")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "server", "static", "markers")
SIZE = 96

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def norm(name):
    n = re.sub(r"\[[^\]]*\]", "", name)          # strip [b][i] bbcode
    return re.sub(r"[^a-z0-9]", "", n.lower())


def clean(name):
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", name)).strip()


def walk(o):
    yield o
    for c in o.get("ContainedObjects") or []:
        yield from walk(c)
    for c in (o.get("States") or {}).values():
        yield from walk(c)


def fetch(url):
    fn = re.sub(r"[^A-Za-z0-9]", "", url)
    for ext in (".png", ".jpg", ".jpeg"):
        p = os.path.join(TTS_IMAGES, fn + ext)
        if os.path.exists(p):
            return open(p, "rb").read()
    req = urllib.request.Request(url, headers={"User-Agent": "snapshotbot-markers"})
    return urllib.request.urlopen(req, timeout=60).read()


def main():
    d = json.load(open(MOD, encoding="utf-8"))
    tokens = {}
    for o in walk({"ContainedObjects": d["ObjectStates"]}):
        n = o.get("Nickname") or ""
        if o.get("Name") != "Custom_Token" or not n:
            continue
        url = (o.get("CustomImage") or {}).get("ImageURL")
        k = norm(n)
        if url and k and k not in tokens:
            tokens[k] = (clean(n), url)

    os.makedirs(OUT_DIR, exist_ok=True)
    index, ok, fail = {}, 0, 0
    for k, (name, url) in sorted(tokens.items()):
        try:
            img = Image.open(io.BytesIO(fetch(url))).convert("RGBA")
            img.thumbnail((SIZE, SIZE))
            img.save(os.path.join(OUT_DIR, k + ".png"), optimize=True)
            index[k] = name
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL {name!r}: {e}")
    with open(os.path.join(OUT_DIR, "markers.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, indent=0, ensure_ascii=False)
    print(f"finished: ok={ok} fail={fail} -> {OUT_DIR}")


if __name__ == "__main__":
    main()
