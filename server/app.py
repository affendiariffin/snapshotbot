import base64
import hmac
import io
import json
import os
import re

from flask import Flask, Response, jsonify, redirect, render_template, request
from PIL import Image, ImageDraw

from server import db, meshgeom
from server.api import ADMIN_KEY, api, is_admin

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
app.register_blueprint(api)

# Boot-time schema + retention sweep (runs under gunicorn too, unlike main()).
if os.environ.get("DATABASE_URL"):
    db.init_db()
    meshgeom.resume_pending()

DISP_CODES = {
    "Take and Hold": "TH", "Purge the Foe": "PF", "Disruption": "DI",
    "Reconnaissance": "RE", "Priority Assets": "PA",
}
# Map-card-name abbreviations (LCT deck naming) — fallback when dispositions are unset.
MAP_ABBREVS = {"TnH": "TH", "PtF": "PF", "Dis": "DI", "Rec": "RE", "Recon": "RE", "PA": "PA"}
CODE_ORDER = ["TH", "PF", "DI", "RE", "PA"]

_layouts_meta = None


def layouts_meta():
    global _layouts_meta
    if _layouts_meta is None:
        path = os.path.join(app.static_folder, "layouts", "layouts_meta.json")
        with open(path, encoding="utf-8") as f:
            _layouts_meta = json.load(f)
    return _layouts_meta


def layout_key(meta):
    codes = []
    for side in ("red_disposition", "blue_disposition"):
        codes.append(DISP_CODES.get(meta.get(side) or ""))
    map_name = meta.get("map") or ""
    if not all(codes):
        found = [MAP_ABBREVS[a] for a in re.findall(r"\b(TnH|PtF|Dis|Recon|Rec|PA)\b", map_name)]
        if len(found) >= 2:
            codes = found[:2]
    if not all(codes):
        return None
    codes.sort(key=CODE_ORDER.index)
    m = re.search(r"\b([123])\b", map_name)
    letter = "ABC"[int(m.group(1)) - 1] if m else "A"
    key = f"{codes[0]}-{codes[1]}-{letter}"
    return key if key in layouts_meta() else None


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "snapshotbot"})


@app.get("/admin/<key>")
def admin_login(key):
    # Fendi's one-click login: sets the admin cookie for a year on this browser.
    if not ADMIN_KEY or not hmac.compare_digest(key, ADMIN_KEY):
        return "Unknown link.", 403
    resp = redirect("/")
    resp.set_cookie("sb_admin", ADMIN_KEY, max_age=365 * 24 * 3600,
                    httponly=True, secure=True, samesite="Lax")
    return resp


def _admin_view():
    # ?guest=1 lets Fendi preview the guest rendering without shedding his cookie.
    # Display-only: the write APIs still honour the cookie, but the guest UI never
    # calls them (read-only textareas, no rename/delete buttons).
    return is_admin() and "guest" not in request.args


@app.get("/")
def index():
    return render_template("index.html", sessions=db.list_sessions(), admin=_admin_view())


def _og_card(bundle, slug):
    # Discord/OG unfurl: players + score in the title, mission facts in the body,
    # the post-deployment board thumbnail as the image.
    meta = bundle["mission_meta"]
    rp, bp = meta.get("red_player") or "Red", meta.get("blue_player") or "Blue"
    last = bundle["snapshots"][-1] if bundle["snapshots"] else {}
    sc = last.get("scores") or {}
    rt = (sc.get("red") or {}).get("total")
    bt = (sc.get("blue") or {}).get("total")
    title = bundle.get("title") or f"{rp} vs {bp}"
    if rt is not None and bt is not None:
        title += f"  —  {rt} : {bt}"
    live = bundle.get("ended_at") is None
    bits = [meta.get("map") or "LCT game",
            ("LIVE — round " if live else "round ") + str(last.get("round") or 0),
            bundle["started_at"][:10]]
    root = request.url_root.replace("http://", "https://")
    return {"title": title, "desc": " · ".join(bits),
            "image": f"{root}r/{slug}/thumb.png"}


def _flip_sign(bundle, key):
    # Same call the viewer makes: if the claimed deployment centroids sit opposite
    # the layout SVG's baked red zone, render the world rotated 180°.
    rz = (layouts_meta().get(key) or {}).get("red_zone")
    if not rz:
        return 1
    dot = 0
    for s in bundle["snapshots"]:
        if (s.get("round") or 0) >= 2:
            break
        for m in s.get("models") or []:
            if not m.get("t") or m.get("v"):
                continue
            dot += (m["x"] * rz[0] + m["z"] * rz[1]) * (1 if m["t"] == "red" else -1)
    return -1 if dot < 0 else 1


_thumb_cache = {}

TEAM_FILL = {"red": (224, 85, 85), "blue": (85, 136, 224), None: (110, 116, 148)}


@app.get("/r/<slug>/thumb.png")
def replay_thumb(slug):
    # Link-preview image: the layout base render + team dots at end of deployment.
    bundle = db.get_session_bundle(slug)
    if bundle is None:
        return "unknown session", 404
    key = layout_key(bundle["mission_meta"])
    base_path = os.path.join(app.static_folder, "layouts", "png", f"{key}.png")
    if not key or not os.path.exists(base_path) or not bundle["snapshots"]:
        return redirect("/static/og-banner.png")
    # the board at the start of the first player turn (fall back to newest available)
    frame = next((s for s in bundle["snapshots"] if (s.get("round") or 0) >= 1),
                 bundle["snapshots"][-1])
    ck = (slug, frame["id"])
    if ck not in _thumb_cache:
        f = _flip_sign(bundle, key)
        img = Image.open(base_path).convert("RGB")
        d = ImageDraw.Draw(img)
        ppi = img.width / 60  # 60x44 inch board
        for m in frame.get("models") or []:
            if m.get("v"):
                continue
            b = m.get("b")
            w = (b[0] if isinstance(b, list) else b) or 1.26
            r = max(min(w / 2 * ppi, 20), 3)
            cx = img.width / 2 + f * m["x"] * ppi
            cy = img.height / 2 - f * m["z"] * ppi
            fill = TEAM_FILL.get(m.get("t"), TEAM_FILL[None])
            line = tuple(min(c + 60, 255) for c in fill)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill, outline=line, width=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        if len(_thumb_cache) > 20:
            _thumb_cache.clear()
        _thumb_cache[ck] = buf.getvalue()
    return Response(_thumb_cache[ck], mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/r/<slug>")
def replay(slug):
    bundle = db.get_session_bundle(slug)
    if bundle is None:
        return "Unknown or expired session (replays keep for 30 days).", 404
    key = layout_key(bundle["mission_meta"])
    lay = layouts_meta().get(key) if key else None
    cards_rev, geom_rev = db.asset_revs()
    return render_template(
        "replay.html",
        slug=slug,
        layout_key=key,
        layout_meta_json=json.dumps(lay or {}, ensure_ascii=False),
        embedded_json=None,
        admin=_admin_view(),
        cards_rev=cards_rev,
        geom_rev=geom_rev,
        og=_og_card(bundle, slug),
    )


@app.get("/r/<slug>/download")
def replay_download(slug):
    # Self-contained HTML export: session data, layout SVG, base-size guide and
    # silhouette PNGs all inlined — survives the 30-day purge, works offline.
    bundle = db.get_session_bundle(slug)
    if bundle is None:
        return "Unknown or expired session (replays keep for 30 days).", 404
    key = layout_key(bundle["mission_meta"])
    lay = layouts_meta().get(key) if key else None
    keys = {m.get("g") for s in bundle["snapshots"] for m in (s.get("models") or []) if m.get("g")}
    layout_svg = None
    if key:
        with open(os.path.join(app.static_folder, "layouts", key + ".svg"), encoding="utf-8") as f:
            layout_svg = f.read()
    with open(os.path.join(app.static_folder, "base_sizes.json"), encoding="utf-8") as f:
        base_sizes = json.load(f)
    card_names = set()
    for s in bundle["snapshots"]:
        cd = s.get("cards") or {}
        for k in ("red_primary", "blue_primary", "red_secondary", "blue_secondary"):
            card_names.update(cd.get(k) or [])
        for c in cd.get("loose") or []:
            if c.get("n"):
                card_names.add(c["n"])
    card_keys = {re.sub(r"[^a-z0-9]", "", re.sub(r"^(a|an|the)\s+", "", n.lower().strip()))
                 for n in card_names}
    with open(os.path.join(app.static_folder, "card_names.json"), encoding="utf-8") as f:
        card_names_json = json.load(f)
    marker_b64 = {}
    for s in bundle["snapshots"]:
        for m in s.get("markers") or []:
            k = re.sub(r"[^a-z0-9]", "", (m.get("n") or "").lower())
            if k and k not in marker_b64:
                p = os.path.join(app.static_folder, "markers", k + ".png")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        marker_b64[k] = base64.b64encode(f.read()).decode()
    embedded = {"session": bundle, "geom": db.geom_export(keys),
                "layout_svg": layout_svg, "base_sizes": base_sizes,
                "cards": db.cards_export(card_keys), "card_names": card_names_json,
                "markers": marker_b64}
    html = render_template(
        "replay.html",
        slug=slug,
        layout_key=key,
        layout_meta_json=json.dumps(lay or {}, ensure_ascii=False),
        # </script> inside embedded strings would terminate the script tag
        embedded_json=json.dumps(embedded, ensure_ascii=False).replace("</", "<\\/"),
        admin=False,
        cards_rev=0,
        geom_rev=0,
    )
    date = bundle["started_at"][:10]
    return Response(html, mimetype="text/html", headers={
        "Content-Disposition": f'attachment; filename="snapshotbot_{date}_{slug}.html"'})


def main():
    db.init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)


if __name__ == "__main__":
    main()
