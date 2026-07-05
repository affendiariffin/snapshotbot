# Harvest the official GDM 2026 secondary-mission card renders from gdmissions.app
# (the mod only ships player-skinned red/blue copies; Fendi wants the official
# faces). Screenshots each card page's card element at 2x and overwrites the
# matching sb_card_images rows. Rerun after GDM updates.
#
# Usage:  DATABASE_URL=<public DSN> python local/gdm_secondaries.py
import io
import os
import re
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import db  # noqa: E402

LIST_URL = "https://gdmissions.app/11th/secondary-missions"
CARD_SEL = "main .bracket.relative.overflow-hidden"
MAX_H = 700


def norm(name):
    n = re.sub(r"^(a|an|the)\s+", "", name.lower().strip())
    return re.sub(r"[^a-z0-9]", "", n)


def main():
    with sync_playwright() as p:
        b = p.firefox.launch()
        page = b.new_page(viewport={"width": 1400, "height": 1300},
                          device_scale_factor=2)
        page.goto(LIST_URL, timeout=60000)
        page.wait_for_timeout(3500)
        slugs = page.evaluate(
            "[...new Set([...document.querySelectorAll('a')].map(a => a.getAttribute('href'))"
            ".filter(h => h && h.match(/secondary-missions\\/.+/)))]")
        print(f"{len(slugs)} card pages")
        ok = fail = 0
        for href in sorted(slugs):
            name = href.rsplit("/", 1)[1].replace("-defender", "").replace("-", " ")
            key = norm(name)
            try:
                page.goto("https://gdmissions.app" + href, timeout=60000)
                page.wait_for_selector(CARD_SEL, timeout=20000)
                page.add_style_tag(content=CARD_SEL + " button{display:none!important}")
                page.wait_for_timeout(1200)
                png = page.locator(CARD_SEL).first.screenshot()
                img = Image.open(io.BytesIO(png)).convert("RGB")
                if img.height > MAX_H:
                    img = img.resize((round(img.width * MAX_H / img.height), MAX_H))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=88, optimize=True)
                db.card_put(key, name.title(), buf.getvalue())   # upserts
                print(f"  ok {key} ({img.width}x{img.height})")
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  FAIL {key}: {e}")
        b.close()
        print(f"finished: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
