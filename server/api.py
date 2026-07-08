import hashlib
import hmac
import json
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

# Concurrent-recording cap (Fendi, 2026-07-05: tokens will spread to friends'
# tables, but the Railway bill is his). A token refused here retries every poll
# and starts recording as soon as a table finishes.
MAX_LIVE = int(os.environ.get("SB_MAX_LIVE", "3"))


def is_admin():
    if not ADMIN_KEY:
        return True
    return hmac.compare_digest(request.cookies.get("sb_admin", ""), ADMIN_KEY)

# Per-IP sliding-window rate limits (teams-pairing pattern). No auth by design:
# unguessable slugs gate reads, these gate writes, 30-day TTL cleans up the rest.
_BUCKETS = {}
_LIMITS = {"start": 5, "snapshot": 90, "notes": 30, "log": 60, "geom": 30, "admin": 20}

GEOM_KEY_RE = re.compile(r"^\d{1,25}(-\d{1,25}){0,6}$")
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


# The served download IS the latest token build; its stamped TOKEN_VERSION is
# the canonical current version. Tokens self-check against this on load.
_token_version = None


def token_version():
    global _token_version
    if _token_version is None:
        path = os.path.join(os.path.dirname(__file__), "static", "snapshotbot-v2.json")
        try:
            with open(path, encoding="utf-8") as f:
                lua = json.load(f)["ObjectStates"][0]["LuaScript"]
            m = re.search(r'TOKEN_VERSION = "([^"]+)"', lua)
            _token_version = m.group(1) if m else "unknown"
        except (OSError, KeyError, IndexError, ValueError):
            _token_version = "unknown"
    return _token_version


@api.get("/api/version")
def version():
    return jsonify({"ok": True, "version": token_version()})


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
    # Respawned-token failsafe: rejoin the interrupted game instead of splitting
    # it across two replays (also exempt from the capacity gate — resuming a
    # recording doesn't add one).
    adopt = db.find_adoptable_session(meta)
    if adopt:
        return jsonify({"ok": True, "slug": adopt, "path": "/r/" + adopt, "resumed": True})
    if db.count_live_sessions() >= MAX_LIVE:
        return _bad(f"at capacity ({MAX_LIVE} games recording) — will retry", 429)
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
    kids = _clean_children(body.get("children"), [0])
    if kids:
        spec["children"] = kids
    else:
        # Legacy flat single-child spec (tokens in the wild predating children[]).
        child = body.get("child_mesh")
        if isinstance(child, str) and child.startswith("http"):
            spec["child_mesh"] = child[:500]
            for k in ("child_rot", "child_x", "child_z", "child_scale"):
                v = body.get(k)
                if isinstance(v, (int, float)) and abs(v) < 1e6:
                    spec[k] = v
    meshgeom.enqueue(key, spec["name"], spec)
    return jsonify({"ok": True})


def _clean_children(nodes, count, depth=1):
    # Mirror of the token's childSpecs walk: ≤6 descendants, ≤3 deep, http meshes,
    # finite numbers. Anything malformed is dropped, not rejected — the parent
    # mesh alone still yields a usable (if worse) silhouette.
    if not isinstance(nodes, list) or depth > 3:
        return None
    out = []
    for n in nodes:
        if count[0] >= 6:
            break
        if not isinstance(n, dict):
            continue
        mesh = n.get("mesh")
        if not isinstance(mesh, str) or not mesh.startswith("http"):
            continue
        count[0] += 1
        node = {"mesh": mesh[:500]}
        for k in ("rot", "x", "z", "scale"):
            v = n.get(k)
            if isinstance(v, (int, float)) and abs(v) < 1e6:
                node[k] = v
        kids = _clean_children(n.get("children"), count, depth + 1)
        if kids:
            node["children"] = kids
        out.append(node)
    return out or None


@api.get("/api/geom/status")
def geom_status():
    keys = [k for k in (request.args.get("keys") or "").split(",") if GEOM_KEY_RE.match(k)]
    if not keys or len(keys) > 300:
        return _bad("bad keys")
    return jsonify({"ok": True, "geom": db.geom_status(keys)})


def _etagged(data, mimetype):
    # Image URLs carry a ?v=<revision> that changes on re-harvest/rebake, so long
    # caching is safe; the ETag covers stragglers hitting unversioned URLs.
    etag = hashlib.md5(data).hexdigest()[:16]
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})
    return Response(data, mimetype=mimetype,
                    headers={"ETag": etag, "Cache-Control": "public, max-age=604800"})


@api.get("/api/card/<key>.jpg")
def card_img(key):
    if not CARD_KEY_RE.match(key):
        return _bad("bad key")
    img = db.card_get(key)
    if img is None:
        return _bad("no such card", 404)
    return _etagged(img, "image/jpeg")


@api.get("/api/geom/<key>.png")
def geom_png(key):
    if not GEOM_KEY_RE.match(key):
        return _bad("bad key")
    png = db.geom_png(key)
    if png is None:
        return _bad("not ready", 404)
    return _etagged(png, "image/png")


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



