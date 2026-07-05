import base64
import os
import secrets

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Retention window for sessions and everything under them (Fendi: 30 days, no archive).
RETENTION_DAYS = 30
# Hard cap on stored sessions (Fendi, 2026-07-05): creating game #11 deletes the
# oldest. Friends who want to keep a game use the self-contained HTML download.
MAX_SESSIONS = 10
# Sessions end by abandonment: the token heartbeats every 60s while TTS is open, so
# one missed beat plus poll jitter means everyone left (no End button by design).
# A false seal (network blip) self-heals: the next snapshot reopens the session.
STALE_SECONDS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS sb_sessions (
    id          BIGSERIAL PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    mission_meta JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS sb_sessions_started_idx ON sb_sessions (started_at);

CREATE TABLE IF NOT EXISTS sb_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    session_id  BIGINT NOT NULL REFERENCES sb_sessions(id) ON DELETE CASCADE,
    taken_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    round       INT NOT NULL DEFAULT 0,
    mark        TEXT,
    scores      JSONB NOT NULL DEFAULT '{}'::jsonb,
    cards       JSONB NOT NULL DEFAULT '{}'::jsonb,
    models      JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS sb_snapshots_session_idx ON sb_snapshots (session_id, id);

CREATE TABLE IF NOT EXISTS sb_notes (
    session_id  BIGINT NOT NULL REFERENCES sb_sessions(id) ON DELETE CASCADE,
    cell_key    TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, cell_key)
);

-- Mission/secondary card face images from the LCT mod (permanent cache; shown in
-- the replay panel instead of linking out to third-party rules sites).
CREATE TABLE IF NOT EXISTS sb_card_images (
    key         TEXT PRIMARY KEY,
    name        TEXT,
    img         BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Mesh geometry cache (permanent, NOT under the 30-day sweep): true base size +
-- top-down silhouette per unique sculpt, keyed by the mesh URL's ugc id.
CREATE TABLE IF NOT EXISTS sb_mesh_geom (
    key         TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    name        TEXT,
    spec        JSONB NOT NULL DEFAULT '{}'::jsonb,
    base        JSONB,
    sil_png     BYTEA,
    sil_meta    JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _dsn():
    url = os.environ["DATABASE_URL"]
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def get_conn():
    return psycopg.connect(_dsn(), row_factory=dict_row)


def init_db():
    with get_conn() as conn:
        conn.execute(SCHEMA)
        conn.execute("ALTER TABLE sb_sessions ADD COLUMN IF NOT EXISTS title TEXT")
        conn.execute("ALTER TABLE sb_snapshots ADD COLUMN IF NOT EXISTS"
                     " markers JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE sb_sessions ADD COLUMN IF NOT EXISTS last_beat_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE sb_snapshots ADD COLUMN IF NOT EXISTS"
                     " resumed BOOLEAN NOT NULL DEFAULT FALSE")
    expire_old_sessions()
    finalize_stale_sessions()


def expire_old_sessions():
    # Hard delete past the retention window; snapshots/notes go with the CASCADE.
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM sb_sessions WHERE started_at < now() - make_interval(days => %s)",
            (RETENTION_DAYS,),
        )
        return cur.rowcount


def create_session(meta):
    with get_conn() as conn:
        for _ in range(5):
            slug = secrets.token_urlsafe(6)
            try:
                conn.execute(
                    "INSERT INTO sb_sessions (slug, mission_meta) VALUES (%s, %s)",
                    (slug, Jsonb(meta)),
                )
                conn.execute(
                    "DELETE FROM sb_sessions WHERE id NOT IN"
                    " (SELECT id FROM sb_sessions ORDER BY started_at DESC LIMIT %s)",
                    (MAX_SESSIONS,),
                )
                return slug
            except psycopg.errors.UniqueViolation:
                conn.rollback()
        raise RuntimeError("could not allocate session slug")


def rename_session(slug, title):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sb_sessions SET title = %s WHERE slug = %s", (title or None, slug)
        )
        return cur.rowcount > 0


def delete_session(slug):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sb_sessions WHERE slug = %s", (slug,))
        return cur.rowcount > 0


def geom_export(keys):
    # For the self-contained HTML download: everything the viewer needs, PNGs included.
    if not keys:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, base, sil_meta, sil_png FROM sb_mesh_geom"
            " WHERE key = ANY(%s) AND status = 'done'",
            (list(keys),),
        ).fetchall()
        return {
            r["key"]: {
                "status": "done", "base": r["base"], "sil_meta": r["sil_meta"],
                "png_b64": base64.b64encode(bytes(r["sil_png"])).decode() if r["sil_png"] else None,
            }
            for r in rows
        }


def _session_id(conn, slug, open_only=False):
    q = "SELECT id FROM sb_sessions WHERE slug = %s"
    if open_only:
        q += " AND ended_at IS NULL"
    row = conn.execute(q, (slug,)).fetchone()
    return row["id"] if row else None


def add_snapshot(slug, round_, mark, scores, cards, models, markers=None):
    # A snapshot for a sealed session reopens it: the seal only means "currently
    # silent", so a returning token (blip recovered, save reloaded) resumes recording.
    # Identical-state posts are the token's 60s liveness heartbeat: they refresh
    # last_beat_at (keeps the 90s seal at bay) but store NO row — long games are
    # mostly thinking time and would otherwise be mostly duplicate frames. A post
    # that reopens a sealed session always stores, flagged resumed=true, so the
    # viewer's turn clocks can zero the away-from-table gap.
    with get_conn() as conn:
        sess = conn.execute(
            "SELECT id, ended_at FROM sb_sessions WHERE slug = %s", (slug,)
        ).fetchone()
        if sess is None:
            return None
        sid, was_sealed = sess["id"], sess["ended_at"] is not None
        conn.execute(
            "UPDATE sb_sessions SET ended_at = NULL, last_beat_at = now() WHERE id = %s",
            (sid,),
        )
        last = conn.execute(
            "SELECT id, round, mark, scores, cards, models, markers FROM sb_snapshots"
            " WHERE session_id = %s ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if (last is not None and mark is None and not was_sealed
                and last["round"] == round_ and last["scores"] == scores
                and last["cards"] == cards and last["models"] == models
                and last["markers"] == (markers or [])):
            return last["id"]
        row = conn.execute(
            "INSERT INTO sb_snapshots (session_id, round, mark, scores, cards, models,"
            " markers, resumed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (sid, round_, mark, Jsonb(scores), Jsonb(cards), Jsonb(models),
             Jsonb(markers or []), was_sealed and last is not None),
        ).fetchone()
        return row["id"]


def finalize_stale_sessions():
    # Seal open sessions whose last activity is older than the stale window. Since
    # heartbeats no longer store rows, activity = last_beat_at (or last snapshot for
    # pre-dedupe sessions); ended_at = when the players actually left.
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE sb_sessions s SET ended_at = GREATEST(
                COALESCE((SELECT max(taken_at) FROM sb_snapshots WHERE session_id = s.id),
                         s.started_at),
                COALESCE(s.last_beat_at, s.started_at))
            WHERE s.ended_at IS NULL
              AND GREATEST(
                COALESCE((SELECT max(taken_at) FROM sb_snapshots WHERE session_id = s.id),
                         s.started_at),
                COALESCE(s.last_beat_at, s.started_at)) < now() - make_interval(secs => %s)
            """,
            (STALE_SECONDS,),
        )
        return cur.rowcount


def get_session_bundle(slug, after_id=0):
    with get_conn() as conn:
        sess = conn.execute(
            "SELECT id, slug, title, started_at, ended_at, mission_meta"
            " FROM sb_sessions WHERE slug = %s",
            (slug,),
        ).fetchone()
        if sess is None:
            return None
        snaps = conn.execute(
            "SELECT id, taken_at, round, mark, scores, cards, models, markers, resumed"
            " FROM sb_snapshots WHERE session_id = %s AND id > %s ORDER BY id",
            (sess["id"], after_id),
        ).fetchall()
        notes = conn.execute(
            "SELECT cell_key, body FROM sb_notes WHERE session_id = %s", (sess["id"],)
        ).fetchall()
        return {
            "slug": sess["slug"],
            "title": sess["title"],
            "started_at": sess["started_at"].isoformat(),
            "ended_at": sess["ended_at"].isoformat() if sess["ended_at"] else None,
            "mission_meta": sess["mission_meta"],
            "snapshots": [
                {
                    "id": s["id"],
                    "taken_at": s["taken_at"].isoformat(),
                    "round": s["round"],
                    "mark": s["mark"],
                    "scores": s["scores"],
                    "cards": s["cards"],
                    "models": s["models"],
                    "markers": s["markers"],
                    "resumed": s["resumed"],
                }
                for s in snaps
            ],
            "notes": {n["cell_key"]: n["body"] for n in notes},
        }


def list_sessions(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.slug, s.title, s.started_at, s.ended_at, s.mission_meta,"
            " count(sn.id) AS snaps, max(sn.round) AS max_round"
            " FROM sb_sessions s LEFT JOIN sb_snapshots sn ON sn.session_id = s.id"
            " GROUP BY s.id ORDER BY s.started_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "slug": r["slug"],
                    "title": r["title"],
                    "started_at": r["started_at"].isoformat(),
                    "ended": r["ended_at"] is not None,
                    "mission_meta": r["mission_meta"],
                    "snaps": r["snaps"],
                    "max_round": r["max_round"] or 0,
                }
            )
        return out


def card_put(key, name, img):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sb_card_images (key, name, img) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, img = EXCLUDED.img,"
            " created_at = now()",   # bumps the asset revision → viewer cache-busts
            (key, name, img),
        )


def asset_revs():
    # Cache-busting stamps: image URLs carry ?v=<rev>, so browsers can cache hard
    # but any re-harvest/rebake changes every URL.
    with get_conn() as conn:
        c = conn.execute("SELECT extract(epoch FROM max(created_at))::bigint AS r"
                         " FROM sb_card_images").fetchone()
        g = conn.execute("SELECT extract(epoch FROM max(created_at))::bigint AS r"
                         " FROM sb_mesh_geom").fetchone()
        return (c["r"] or 0, g["r"] or 0)


def card_get(key):
    with get_conn() as conn:
        row = conn.execute("SELECT img FROM sb_card_images WHERE key = %s", (key,)).fetchone()
        return bytes(row["img"]) if row else None


def card_keys():
    with get_conn() as conn:
        return {r["key"] for r in conn.execute("SELECT key FROM sb_card_images").fetchall()}


def cards_export(keys):
    if not keys:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, img FROM sb_card_images WHERE key = ANY(%s)", (list(keys),)
        ).fetchall()
        return {r["key"]: base64.b64encode(bytes(r["img"])).decode() for r in rows}


def geom_upsert(key, name, spec):
    # True if the key needs processing (new, or a previous attempt failed).
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM sb_mesh_geom WHERE key = %s", (key,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO sb_mesh_geom (key, name, spec) VALUES (%s, %s, %s)"
                " ON CONFLICT (key) DO NOTHING",
                (key, name, Jsonb(spec)),
            )
            return True
        if row["status"] == "failed":
            conn.execute(
                "UPDATE sb_mesh_geom SET status = 'pending', spec = %s, error = NULL"
                " WHERE key = %s AND status = 'failed'",
                (Jsonb(spec), key),
            )
            return True
        return False


def geom_claim(key):
    with get_conn() as conn:
        row = conn.execute(
            "UPDATE sb_mesh_geom SET status = 'working' WHERE key = %s"
            " AND status = 'pending' RETURNING spec",
            (key,),
        ).fetchone()
        return row["spec"] if row else None


def geom_finish(key, base, png, meta):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sb_mesh_geom SET status = 'done', base = %s, sil_png = %s,"
            " sil_meta = %s, error = NULL WHERE key = %s",
            (Jsonb(base), png, Jsonb(meta), key),
        )


def geom_fail(key, err):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sb_mesh_geom SET status = 'failed', error = %s WHERE key = %s",
            (err, key),
        )


def geom_stuck():
    # pending = never started; working >10min = a redeploy killed the thread mid-run.
    with get_conn() as conn:
        rows = conn.execute(
            "UPDATE sb_mesh_geom SET status = 'pending' WHERE status = 'pending'"
            " OR (status = 'working' AND created_at < now() - interval '10 minutes')"
            " RETURNING key"
        ).fetchall()
        return [r["key"] for r in rows]


def geom_done_keys():
    with get_conn() as conn:
        rows = conn.execute("SELECT key FROM sb_mesh_geom WHERE status = 'done'").fetchall()
        return {r["key"] for r in rows}


def geom_put_done(key, name, spec, base, png, meta, overwrite=False):
    # Local pre-crunch upload path: lands finished rows directly. Without overwrite
    # it never downgrades an existing done row; overwrite is for rebakes.
    guard = "" if overwrite else " WHERE sb_mesh_geom.status <> 'done'"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sb_mesh_geom (key, status, name, spec, base, sil_png, sil_meta)"
            " VALUES (%s, 'done', %s, %s, %s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET status = 'done', name = EXCLUDED.name,"
            " spec = EXCLUDED.spec, base = EXCLUDED.base, sil_png = EXCLUDED.sil_png,"
            " sil_meta = EXCLUDED.sil_meta, error = NULL, created_at = now()" + guard,
            (key, name, Jsonb(spec), Jsonb(base), png, Jsonb(meta)),
        )


def geom_status(keys):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, status, base, sil_meta FROM sb_mesh_geom WHERE key = ANY(%s)",
            (keys,),
        ).fetchall()
        return {
            r["key"]: {"status": r["status"], "base": r["base"], "sil_meta": r["sil_meta"]}
            for r in rows
        }


def geom_png(key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sil_png FROM sb_mesh_geom WHERE key = %s AND status = 'done'",
            (key,),
        ).fetchone()
        return bytes(row["sil_png"]) if row and row["sil_png"] else None


def save_note(slug, cell_key, body):
    with get_conn() as conn:
        sid = _session_id(conn, slug)
        if sid is None:
            return False
        conn.execute(
            "INSERT INTO sb_notes (session_id, cell_key, body, updated_at)"
            " VALUES (%s, %s, %s, now())"
            " ON CONFLICT (session_id, cell_key) DO UPDATE SET body = %s, updated_at = now()",
            (sid, cell_key, body, body),
        )
        return True
