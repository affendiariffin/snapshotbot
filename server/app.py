import json
import os
import re

from flask import Flask, jsonify, render_template

from server import db
from server.api import api

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
app.register_blueprint(api)

# Boot-time schema + retention sweep (runs under gunicorn too, unlike main()).
if os.environ.get("DATABASE_URL"):
    db.init_db()

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


@app.get("/")
def index():
    return render_template("index.html", sessions=db.list_sessions())


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
    )


def main():
    db.init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)


if __name__ == "__main__":
    main()
