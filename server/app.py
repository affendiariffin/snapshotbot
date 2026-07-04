import hmac
import json
import os
import re

from flask import Flask, Response, jsonify, redirect, render_template

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


@app.get("/")
def index():
    return render_template("index.html", sessions=db.list_sessions(), admin=is_admin())


@app.get("/r/<slug>")
def replay(slug):
    bundle = db.get_session_bundle(slug)
    if bundle is None:
        return "Unknown or expired session (replays keep for 30 days).", 404
    key = layout_key(bundle["mission_meta"])
    lay = layouts_meta().get(key) if key else None
    return render_template(
        "replay.html",
        slug=slug,
        layout_key=key,
        layout_meta_json=json.dumps(lay or {}, ensure_ascii=False),
        embedded_json=None,
        admin=is_admin(),
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
    embedded = {"session": bundle, "geom": db.geom_export(keys),
                "layout_svg": layout_svg, "base_sizes": base_sizes,
                "cards": db.cards_export(card_keys)}
    html = render_template(
        "replay.html",
        slug=slug,
        layout_key=key,
        layout_meta_json=json.dumps(lay or {}, ensure_ascii=False),
        # </script> inside embedded strings would terminate the script tag
        embedded_json=json.dumps(embedded, ensure_ascii=False).replace("</", "<\\/"),
        admin=False,
    )
    date = bundle["started_at"][:10]
    return Response(html, mimetype="text/html", headers={
        "Content-Disposition": f'attachment; filename="snapshotbot_{date}_{slug}.html"'})


def main():
    db.init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)


if __name__ == "__main__":
    main()
