import time

from flask import Blueprint, jsonify, request

from server import db

api = Blueprint("api", __name__)

# Per-IP sliding-window rate limits (teams-pairing pattern). No auth by design:
# unguessable slugs gate reads, these gate writes, 30-day TTL cleans up the rest.
_BUCKETS = {}
_LIMITS = {"start": 5, "snapshot": 90, "notes": 30, "log": 60}

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
    mark = body.get("mark")
    round_ = body.get("round") or 0
    if not isinstance(scores, dict) or not isinstance(cards, dict) or not isinstance(models, list):
        return _bad("scores/cards must be objects, models must be a list")
    if len(models) > 500:
        return _bad("too many models")
    if mark is not None and (not isinstance(mark, str) or len(mark) > 100):
        return _bad("bad mark")
    if not isinstance(round_, int) or not 0 <= round_ <= 10:
        return _bad("bad round")
    snap_id = db.add_snapshot(slug, round_, mark, scores, cards, models)
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


@api.post("/api/notes")
def save_note():
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



