import hmac
import os
import re
import time

from flask import Blueprint, Response, jsonify, request

from server import db, meshgeom

api = Blueprint("api", __name__)

# Single-admin security (Fendi, 2026-07-05): only the holder of the admin cookie
# may rename/delete sessions or edit notes; everyone else is read-only + download.
# Cookie is set by visiting /admin/<key> once per browser. Unset key = open mode
# (local dev); token-facing endpoints (snapshot/geom/log) stay open by design.
ADMIN_KEY = os.environ.get("SB_ADMIN_KEY", "")


def is_admin():
    if not ADMIN_KEY:
        return True
    return hmac.compare_digest(request.cookies.get("sb_admin", ""), ADMIN_KEY)

# Per-IP sliding-window rate limits (teams-pairing pattern). No auth by design:
# unguessable slugs gate reads, these gate writes, 30-day TTL cleans up the rest.
_BUCKETS = {}
_LIMITS = {"start": 5, "snapshot": 90, "notes": 30, "log": 60, "geom": 30, "admin": 20}

GEOM_KEY_RE = re.compile(r"^\d{1,25}(-\d{1,25})?$")
CARD_KEY_RE = re.compile(r"^[a-z0-9]{1,80}$")

NOTE_KEYS = {"deployment", "round1", "round2", "round3", "round4", "round5",
             "army_red", "army_blue"}


def _rate_limited(bucket):
    key = (bucket, request.headers.get("X-Forwarded-For", request.remote_addr or "?"))
    now = time.time()
    hits = [t for t in _BUCKETS.get(key, []) if now - t < 60]
    if len(hits) >= _LIMITS[bucket]:
        _BUCKETS[key] = hits
        return True
    hits.append(now)
    _BUCKETS[key] = hits
    return False


def _bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


@api.post("/api/session/start")
def session_start():
    if _rate_limited("start"):
        return _bad("rate limited", 429)
    body = request.get_json(force=True, silent=True) or {}
    meta = body.get("mission_meta") or {}
    if not isinstance(meta, dict):
        return _bad("mission_meta must be an object")
    db.expire_old_sessions()
    db.finalize_stale_sessions()
    slug = db.create_session(meta)
    return jsonify({"ok": True, "slug": slug, "path": "/r/" + slug})


@api.post("/api/snapshot")
def snapshot():
    if _rate_limited("snapshot"):
        return _bad("rate limited", 429)
    body = request.get_json(force=True, silent=True) or {}
    slug = body.get("slug") or ""
    scores = body.get("scores") or {}
    cards = body.get("cards") or {}
    models = body.get("models") or []
    markers = body.get("markers") or []
    mark = body.get("mark")
    round_ = body.get("round") or 0
    if not isinstance(scores, dict) or not isinstance(cards, dict) or not isinstance(models, list):
        return _bad("scores/cards must be objects, models must be a list")
    if not isinstance(markers, list) or len(markers) > 100:
        return _bad("bad markers")
    if len(models) > 500:
        return _bad("too many models")
    if mark is not None and (not isinstance(mark, str) or len(mark) > 100):
        return _bad("bad mark")
    if not isinstance(round_, int) or not 0 <= round_ <= 10:
        return _bad("bad round")
    snap_id = db.add_snapshot(slug, round_, mark, scores, cards, models, markers)
    if snap_id is None:
        return _bad("unknown or ended session", 404)
    return jsonify({"ok": True, "snapshot_id": snap_id})


@api.get("/api/session/<slug>/data")
def session_data(slug):
    # Live viewers poll this; finalizing here drops the LIVE badge ~90s after the
    # last player leaves TTS, without waiting for the next session create.
    db.finalize_stale_sessions()
    after_id = request.args.get("after", 0, type=int)
    bundle = db.get_session_bundle(slug, after_id)
    if bundle is None:
        return _bad("unknown session", 404)
    return jsonify({"ok": True, "session": bundle})


@api.post("/api/log")
def client_log():
    # Token debug channel: lines land in Railway's log stream (`railway logs`).
    # This project historically needs a LOT of in-TTS troubleshooting.
    if _rate_limited("log"):
        return _bad("rate limited", 429)
    body = request.get_json(force=True, silent=True) or {}
    level = str(body.get("level") or "info")[:10]
    msg = str(body.get("msg") or "")[:500]
    slug = str(body.get("slug") or "-")[:32]
    guid = str(body.get("guid") or "-")[:12]
    print(f"[tts:{level}] session={slug} token={guid} {msg}", flush=True)
    return jsonify({"ok": True})


@api.post("/api/session/<slug>/rename")
def session_rename(slug):
    if not is_admin():
        return _bad("read-only", 403)
    if _rate_limited("admin"):
        return _bad("rate limited", 429)
    body = request.get_json(force=True, silent=True) or {}
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 80):
        return _bad("bad title")
    if not db.rename_session(slug, (title or "").strip()):
        return _bad("unknown session", 404)
    return jsonify({"ok": True})


@api.post("/api/session/<slug>/delete")
def session_delete(slug):
    if not is_admin():
        return _bad("read-only", 403)
    if _rate_limited("admin"):
        return _bad("rate limited", 429)
    if not db.delete_session(slug):
        return _bad("unknown session", 404)
    return jsonify({"ok": True})


@api.post("/api/geom")
def geom_submit():
    # Token posts each new model's mesh spec once per session; the worker downloads
    # the sculpt ONCE EVER (cached forever by key) and computes base + silhouette.
    if _rate_limited("geom"):
        return _bad("rate limited", 429)
    body = request.get_json(force=True, silent=True) or {}
    key = str(body.get("key") or "")
    if not GEOM_KEY_RE.match(key):
        return _bad("bad key")
    mesh = body.get("mesh")
    if not isinstance(mesh, str) or not mesh.startswith("http"):
        return _bad("bad mesh url")
    spec = {"mesh": mesh[:500], "name": str(body.get("name") or "")[:100]}
    child = body.get("child_mesh")
    if isinstance(child, str) and child.startswith("http"):
        spec["child_mesh"] = child[:500]
        for k in ("child_rot", "child_x", "child_z", "child_scale"):
            v = body.get(k)
            if isinstance(v, (int, float)) and abs(v) < 1e6:
                spec[k] = v
    meshgeom.enqueue(key, spec["name"], spec)
    return jsonify({"ok": True})


@api.get("/api/geom/status")
def geom_status():
    keys = [k for k in (request.args.get("keys") or "").split(",") if GEOM_KEY_RE.match(k)]
    if not keys or len(keys) > 300:
        return _bad("bad keys")
    return jsonify({"ok": True, "geom": db.geom_status(keys)})


@api.get("/api/card/<key>.jpg")
def card_img(key):
    if not CARD_KEY_RE.match(key):
        return _bad("bad key")
    img = db.card_get(key)
    if img is None:
        return _bad("no such card", 404)
    return Response(img, mimetype="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@api.get("/api/geom/<key>.png")
def geom_png(key):
    if not GEOM_KEY_RE.match(key):
        return _bad("bad key")
    png = db.geom_png(key)
    if png is None:
        return _bad("not ready", 404)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@api.post("/api/notes")
def save_note():
    if not is_admin():
        return _bad("read-only", 403)
    if _rate_limited("notes"):
        return _bad("rate limited", 429)
    body = request.get_json(force=True, silent=True) or {}
    key = body.get("cell_key") or ""
    text = body.get("body")
    if key not in NOTE_KEYS:
        return _bad("bad cell_key")
    if not isinstance(text, str) or len(text) > 20000:
        return _bad("bad body")
    if not db.save_note(body.get("slug") or "", key, text):
        return _bad("unknown session", 404)
    return jsonify({"ok": True})



