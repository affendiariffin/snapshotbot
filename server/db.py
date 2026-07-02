import os
import secrets

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Retention window for sessions and everything under them (Fendi: 30 days, no archive).
RETENTION_DAYS = 30
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
                return slug
            except psycopg.errors.UniqueViolation:
                conn.rollback()
        raise RuntimeError("could not allocate session slug")


def _session_id(conn, slug, open_only=False):
    q = "SELECT id FROM sb_sessions WHERE slug = %s"
    if open_only:
        q += " AND ended_at IS NULL"
    row = conn.execute(q, (slug,)).fetchone()
    return row["id"] if row else None


def add_snapshot(slug, round_, mark, scores, cards, models):
    # A snapshot for a sealed session reopens it: the seal only means "currently
    # silent", so a returning token (blip recovered, save reloaded) resumes recording.
    with get_conn() as conn:
        sid = _session_id(conn, slug)
        if sid is None:
            return None
        conn.execute("UPDATE sb_sessions SET ended_at = NULL WHERE id = %s", (sid,))
        row = conn.execute(
            "INSERT INTO sb_snapshots (session_id, round, mark, scores, cards, models)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (sid, round_, mark, Jsonb(scores), Jsonb(cards), Jsonb(models)),
        ).fetchone()
        return row["id"]


def finalize_stale_sessions():
    # Seal open sessions whose last activity is older than the stale window; ended_at
    # becomes the last snapshot time (or started_at for empty sessions).
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE sb_sessions s SET ended_at = COALESCE(
                (SELECT max(taken_at) FROM sb_snapshots WHERE session_id = s.id),
                s.started_at)
            WHERE s.ended_at IS NULL
              AND COALESCE(
                (SELECT max(taken_at) FROM sb_snapshots WHERE session_id = s.id),
                s.started_at) < now() - make_interval(secs => %s)
            """,
            (STALE_SECONDS,),
        )
        return cur.rowcount


def get_session_bundle(slug, after_id=0):
    with get_conn() as conn:
        sess = conn.execute(
            "SELECT id, slug, started_at, ended_at, mission_meta FROM sb_sessions WHERE slug = %s",
            (slug,),
        ).fetchone()
        if sess is None:
            return None
        snaps = conn.execute(
            "SELECT id, taken_at, round, mark, scores, cards, models FROM sb_snapshots"
            " WHERE session_id = %s AND id > %s ORDER BY id",
            (sess["id"], after_id),
        ).fetchall()
        notes = conn.execute(
            "SELECT cell_key, body FROM sb_notes WHERE session_id = %s", (sess["id"],)
        ).fetchall()
        return {
            "slug": sess["slug"],
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
                }
                for s in snaps
            ],
            "notes": {n["cell_key"]: n["body"] for n in notes},
        }


def list_sessions(limit=100):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.slug, s.started_at, s.ended_at, s.mission_meta,"
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
                    "started_at": r["started_at"].isoformat(),
                    "ended": r["ended_at"] is not None,
                    "mission_meta": r["mission_meta"],
                    "snaps": r["snaps"],
                    "max_round": r["max_round"] or 0,
                }
            )
        return out


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
