"""
storage — run-history persistence with two interchangeable backends.

  * DATABASE_URL set  -> Postgres (snapshots as JSONB rows).  Used on Render,
                         where the filesystem is ephemeral and would otherwise
                         lose all history on every deploy/restart.
  * DATABASE_URL unset -> local JSON files in runs/ (handy for local dev).

Both expose the same tiny API: init(), save_run(doc), list_runs(venue),
get_run(rid), latest_for(venue). Runs are returned oldest-first; the server
reverses for display.
"""

import os
import json
import pathlib

DB_URL = os.environ.get("DATABASE_URL")

if DB_URL:
    # --- Postgres backend ---------------------------------------------------
    import psycopg
    from psycopg.types.json import Json

    # Render hands out postgres:// ; libpq/psycopg also accept postgresql://
    _URL = DB_URL.replace("postgres://", "postgresql://", 1)
    BACKEND = "postgres"

    def _conn():
        # prepare_threshold=None disables server-side prepared statements, which
        # are incompatible with transaction-mode poolers (Supabase Supavisor on
        # port 6543, PgBouncer). Harmless on a direct connection too.
        return psycopg.connect(_URL, autocommit=True, prepare_threshold=None)

    def init():
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id         text PRIMARY KEY,
                    venue      text NOT NULL,
                    generated  text,
                    doc        jsonb NOT NULL,
                    created_at timestamptz DEFAULT now()
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS runs_venue_idx ON runs (venue, created_at)")

    def save_run(doc: dict):
        with _conn() as c:
            c.execute(
                "INSERT INTO runs (id, venue, generated, doc) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (doc["id"], doc["venue"], doc.get("generated"), Json(doc)),
            )

    def list_runs(venue: str | None = None) -> list[dict]:
        q, args = "SELECT doc FROM runs", ()
        if venue:
            q += " WHERE venue = %s"
            args = (venue,)
        q += " ORDER BY created_at ASC"
        with _conn() as c:
            return [row[0] for row in c.execute(q, args).fetchall()]

    def get_run(rid: str) -> dict | None:
        with _conn() as c:
            row = c.execute("SELECT doc FROM runs WHERE id = %s", (rid,)).fetchone()
            return row[0] if row else None

else:
    # --- local file backend -------------------------------------------------
    RUNS = pathlib.Path(__file__).parent / "runs"
    RUNS.mkdir(exist_ok=True)
    BACKEND = "files"

    def init():
        pass

    def save_run(doc: dict):
        (RUNS / f"{doc['id']}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def list_runs(venue: str | None = None) -> list[dict]:
        out = []
        for p in sorted(RUNS.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if venue and d.get("venue") != venue:
                continue
            out.append(d)
        return out

    def get_run(rid: str) -> dict | None:
        p = RUNS / f"{rid}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


def latest_for(venue: str) -> dict | None:
    runs = list_runs(venue)
    return runs[-1] if runs else None
