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


def walk(o, container=None):
    yield o, container
    for c in o.get("ContainedObjects") or []:
        yield from walk(c, o)
    for c in (o.get("States") or {}).values():
        yield from walk(c, container)


def img_id(url):
    # Last 12 chars of the URL's final path segment — MUST mirror the token
    # Lua's markerImgIds derivation exactly.
    m = re.search(r"(\w+)/*$", url or "")
    return m.group(1).lower()[-12:] if m else None


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
    # Two indexes. Name-keyed (legacy fallback for records without an image id):
    # a token whose dispensing bag carries the SAME name wins the key, so a
    # mod misnaming elsewhere can't steal it. Image-keyed ("i" + 12-char url
    # id, matching the token Lua): the truth — LCT's Objective Secured Red/Blue
    # bags dispense tokens NICKNAMED "Action", so names lie; display name comes
    # from the bag when it disagrees with the token.
    tokens = {}      # namekey -> (display, url, bag_consistent)
    by_img = {}      # "i<id>" -> (display, url, bag_named)
    for o, container in walk({"ContainedObjects": d["ObjectStates"]}):
        n = o.get("Nickname") or ""
        if o.get("Name") != "Custom_Token" or not n:
            continue
        url = (o.get("CustomImage") or {}).get("ImageURL")
        k = norm(n)
        if not url or not k:
            continue
        bag = clean((container or {}).get("Nickname") or "") \
            if "Bag" in ((container or {}).get("Name") or "") else ""
        consistent = bool(bag) and norm(bag) == k
        if k not in tokens or (consistent and not tokens[k][2]):
            tokens[k] = (clean(n), url, consistent)
        iid = img_id(url)
        display = bag if bag and norm(bag) != k else clean(n)
        bag_named = bool(bag) and norm(bag) != k
        if iid and ("i" + iid not in by_img or (bag_named and not by_img["i" + iid][2])):
            by_img["i" + iid] = (display, url, bag_named)

    os.makedirs(OUT_DIR, exist_ok=True)
    index, ok, fail = {}, 0, 0
    todo = {k: (name, url) for k, (name, url, _) in tokens.items()}
    todo.update({k: (name, url) for k, (name, url, _) in by_img.items()})
    for k, (name, url) in sorted(todo.items()):
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
