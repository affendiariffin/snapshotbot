# P4 -- rasterise the 45 layout SVGs to server/static/layouts/png/<KEY>.png.
#
# Offline, re-runnable, a KEEPER. These are the bases the Discord thumbnail compositor draws on
# (app.py thumb.png / frame.png), so they must be the SAME pixels the viewer paints -- render
# them from the shipped SVG, never from a second drawing path that can drift.
#
# FIREFOX, NOT MuPDF, AND THAT IS THE POINT. MuPDF disagrees with browsers on two things this
# artwork actually uses: it will not stroke a <polygon>'s implicit closing edge, and it renders
# rgba() as black. The emitter avoids both (explicit closing vertex, fill-opacity), but the
# viewer is a browser, so the bake has to be one too or the check is not checking the thing that
# ships. Playwright's bundled Firefox is already installed for this repo.
#
# Rendered at 2x and downsampled: the 2px white area borders and the 1.5px feature outlines
# alias badly at 600x440 direct.

import io
import os
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAYDIR = os.path.join(ROOT, "server", "static", "layouts")
PNGDIR = os.path.join(LAYDIR, "png")

OUT_W, OUT_H = 600, 440
SCALE = 2


def main():
    keys = sorted(f[:-4] for f in os.listdir(LAYDIR) if f.endswith(".svg"))
    if not keys:
        print("no SVGs in %s" % LAYDIR)
        return 1
    os.makedirs(PNGDIR, exist_ok=True)
    problems, sizes = [], []
    with sync_playwright() as pw:
        browser = pw.firefox.launch()
        page = browser.new_page(viewport={"width": OUT_W * SCALE, "height": OUT_H * SCALE})
        for key in keys:
            src = os.path.join(LAYDIR, key + ".svg").replace("\\", "/")
            page.goto("file:///" + src)
            shot = page.screenshot(type="png")
            im = Image.open(io.BytesIO(shot)).convert("RGB")
            im = im.resize((OUT_W, OUT_H), Image.LANCZOS)
            # Gate: a blank or near-blank board means the SVG failed to render, which is
            # exactly the failure mode a "file written, exit 0" check cannot see.
            extremes = im.convert("L").getextrema()
            if extremes[1] - extremes[0] < 40:
                problems.append("%s: rendered nearly flat (range %s)" % (key, extremes))
            dest = os.path.join(PNGDIR, key + ".png")
            im.save(dest)
            sizes.append(os.path.getsize(dest))
        browser.close()

    print("layout PNGs written  : %d  at %dx%d (rendered %dx and downsampled)"
          % (len(keys), OUT_W, OUT_H, SCALE))
    print("png size             : min %d  median %d  max %d bytes"
          % (min(sizes), sorted(sizes)[len(sizes) // 2], max(sizes)))
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems[:20]:
            print("   " + p)
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
