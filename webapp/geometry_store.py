"""
geometry_store — append-only persistence for per-run geometry snapshots.

Distinct from storage.py (which persists the tabular run doc — name/unit/
floor/status/category — to Postgres or local JSON depending on deployment).
This module is specifically the geometry timeline: every run's store
polygons, kept forever under webapp/runs/geometry/<venue>/<run_id>.json so
geodiff.py always has a "previous" snapshot to compare the "latest" one
against. Local-JSON only for now (matches this repo's existing dev-storage
convention) — migrating to GeoPackage/PostGIS is future work once snapshot
volume/query needs justify it.
"""

import json
import pathlib

import mallcore

BASE = pathlib.Path(__file__).parent
GEOMETRY_DIR = BASE / "runs" / "geometry"

_GEOMETRY_STORE_KEYS = (
    "location_in_outlet", "geometry", "geometry_crs", "geometry_hash", "source_geometry",
)


def _venue_dir(venue: str) -> pathlib.Path:
    d = GEOMETRY_DIR / mallcore.venue_id_component(venue)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_geometry_record(s: dict) -> dict:
    return {
        "id": s.get("location_in_outlet") or s.get("name"),
        "name": s.get("name"),
        "unit": s.get("location_in_outlet"),
        "floor": s.get("floor"),
        "geometry": s.get("geometry"),
        "geometry_crs": s.get("geometry_crs"),
        "geometry_hash": s.get("geometry_hash"),
        "source_geometry": s.get("source_geometry"),
    }


def save_snapshot(venue: str, run_id: str, stores: list[dict]) -> pathlib.Path:
    """Writes one immutable {run_id, venue, stores:[...]} file. Never
    overwrites an existing run id — a repeat call for the same run_id is a
    no-op (the run id already carries a timestamp, so this only matters for
    accidental re-invocation, not normal operation)."""
    path = _venue_dir(venue) / f"{run_id}.json"
    if path.exists():
        return path
    doc = {
        "run_id": run_id,
        "venue": venue,
        "stores": [_store_geometry_record(s) for s in stores],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def list_snapshots(venue: str) -> list[str]:
    """Run ids for this venue, oldest first (run ids are lexicographically
    sortable timestamps, same convention as server.py's rid format)."""
    d = GEOMETRY_DIR / mallcore.venue_id_component(venue)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def load_snapshot(venue: str, run_id: str) -> dict | None:
    path = GEOMETRY_DIR / mallcore.venue_id_component(venue) / f"{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def latest_snapshot(venue: str) -> dict | None:
    ids = list_snapshots(venue)
    return load_snapshot(venue, ids[-1]) if ids else None


def previous_snapshot(venue: str) -> dict | None:
    """Second-most-recent snapshot — what the just-saved latest gets diffed
    against. None on a venue's first (or only) run."""
    ids = list_snapshots(venue)
    return load_snapshot(venue, ids[-2]) if len(ids) >= 2 else None
