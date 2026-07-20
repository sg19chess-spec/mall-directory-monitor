"""
Mall Directory dashboard — Flask backend.

Endpoints:
  GET  /                     -> the dashboard (static/index.html)
  GET  /api/venues           -> configured venue slugs (venues.json)
  GET  /api/runs?venue=      -> run history (newest first), light metadata
  GET  /api/run/<id>         -> one full run snapshot
  POST /api/run {venue}      -> scrape now, diff vs previous, save snapshot, return it
  GET  /api/capabilities     -> which optional features are configured (e.g. reconciliation)
  POST /api/reconcile {venue}-> fresh primary-source pull vs a live scrape of the
                                 mall's own directory page; surfaces differences in
                                 BOTH directions (see reconcile.py)
  GET  /api/run/<id>/shapes.geojson  -> that run's store footprint polygons (QGIS-ready)
  GET  /api/run/<id>/changes.geojson -> geometry diff vs the previous run for this venue
                                        (expansions/removals/splits/merges — see geodiff.py)
  GET  /api/store/<id>/history        -> one store's footprint area over time

Run history is persisted via storage.py: Postgres when DATABASE_URL is set
(Render), else local JSON files (dev). That history is the log — nothing is
overwritten, so "Previous runs" browses the full timeline.

Geometry snapshots are a separate, always-local-JSON timeline (geometry_store.py)
that geodiff.py compares run-to-run to detect footprint changes — see
mallcore.py's parse_*_venue() functions for where each venue's geometry comes
from (real lon/lat for Mappedin, local pixel space for Mapplic, none yet for
Mall of America).

Local:   pip install -r requirements.txt ; python server.py -> http://127.0.0.1:5000
Render:  gunicorn server:app --bind 0.0.0.0:$PORT   (see render.yaml)
"""

import os
import time
import json
import queue
import pathlib
import datetime
import threading
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context

import mallcore
import storage
import reconcile as reconcile_mod
import geometry_store
import geodiff
import geoexport

BASE = pathlib.Path(__file__).parent
STATIC = BASE / "static"

storage.init()
app = Flask(__name__, static_folder=None)


def load_venues() -> list[str]:
    f = BASE / "venues.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return ["simon-albertville"]


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/api/venues")
def api_venues():
    return jsonify([mallcore.venue_info(v) for v in load_venues()])


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "storage": storage.BACKEND})


@app.get("/api/capabilities")
def api_capabilities():
    return jsonify({"reconciliation": reconcile_mod.available()})


@app.get("/api/runs")
def api_runs():
    venue = request.args.get("venue")
    metas = []
    for d in storage.list_runs(venue):
        ch = d.get("changes") or {}
        rec = d.get("reconciliation") or {}
        metas.append({
            "id": d.get("id"),
            "venue": d.get("venue"),
            # older runs predate generated_utc; fall back to their stored string
            "generated": (mallcore.to_ist_display(d["generated_utc"])
                          if d.get("generated_utc") else d.get("generated")),
            "count": len(d.get("stores", [])),
            "added": len(ch.get("added", [])),
            "removed": len(ch.get("removed", [])),
            "moved": len(ch.get("moved", [])),
            "missing": len(rec.get("missing_from_api", [])) if rec else 0,
            "extra": len(rec.get("extra_in_api", [])) if rec else 0,
        })
    metas.reverse()  # newest first
    return jsonify(metas)


@app.get("/api/run/<rid>")
def api_run(rid):
    doc = storage.get_run(rid)
    if doc is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(doc)


@app.post("/api/run")
def api_do_run():
    body = request.get_json(silent=True) or {}
    venue = body.get("venue") or load_venues()[0]
    is_moa = mallcore.is_moa_venue(venue)
    is_mapplic = mallcore.is_mapplic_venue(venue)
    try:
        if is_moa:
            stores = mallcore.parse_moa_venue(venue)
        elif is_mapplic:
            stores = mallcore.parse_mapplic_venue(venue)
        else:
            stores = mallcore.parse_mappedin_venue(venue)
    except Exception as e:
        return jsonify({"error": f"Scrape failed for '{venue}': {e}"}), 502

    prev = storage.latest_for(venue)
    changes = mallcore.diff_stores(prev.get("stores") if prev else None, stores) if prev else None

    now = datetime.datetime.now()
    rid = f"{mallcore.venue_id_component(venue)}__{now.strftime('%Y%m%d-%H%M%S')}"
    method_prefix = "moa_api" if is_moa else ("mapplic_csv" if is_mapplic else "mappedin_api")

    geometry_store.save_snapshot(venue, rid, stores)
    geometry_changes = geodiff.diff_snapshots(geometry_store.previous_snapshot(venue),
                                               geometry_store.latest_snapshot(venue))

    doc = {
        "id": rid,
        "venue": venue,
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "method": f"{method_prefix}:{venue}",
        "stores": stores,
        "changes": changes,
        "geometry_changes": geometry_changes,
    }
    storage.save_run(doc)
    return jsonify(doc)


@app.get("/api/run-stream")
def api_run_stream():
    """The single 'Run check' action, streamed as Server-Sent Events so the UI
    can narrate every stage live instead of showing an opaque spinner.

    GET (not POST) because EventSource only does GET. Runs the full pipeline:
      primary-source snapshot -> save -> diff vs previous -> reconcile vs mall site.
    Reconciliation is best-effort: if it's unconfigured or the bot wall wins,
    the snapshot is still saved and returned. It's a trust layer, not a gate.
    """
    venue = request.args.get("venue") or load_venues()[0]

    def sse(event: str, **data) -> str:
        return f"data: {json.dumps({'event': event, **data})}\n\n"

    @stream_with_context
    def gen():
        t0 = time.perf_counter()

        def ms():
            return round((time.perf_counter() - t0) * 1000)

        # The pipeline runs on a worker thread and pushes events onto this
        # queue; the response generator drains it. This is what makes the
        # console genuinely live — emitting only after each call returned
        # would just dump every line at the end.
        q: "queue.Queue[tuple | None]" = queue.Queue()

        def emit(event, **data):
            q.put((event, data))

        def progress(step, msg, **extra):
            emit("step", step=step, msg=msg, ms=ms(), **extra)

        def worker():
            try:
                emit("start", venue=venue, info=mallcore.venue_info(venue), ms=ms())

                is_moa = mallcore.is_moa_venue(venue)
                is_mapplic = mallcore.is_mapplic_venue(venue)
                phase = "moa" if is_moa else ("mapplic" if is_mapplic else "mappedin")

                # --- Source A: the mall's map data (fast, primary) -------
                emit("phase", phase=phase, title="Reading the mall's map data", ms=ms())
                if is_moa:
                    stores = mallcore.parse_moa_venue(venue, on_progress=progress)
                elif is_mapplic:
                    stores = mallcore.parse_mapplic_venue(venue, on_progress=progress)
                else:
                    stores = mallcore.parse_mappedin_venue(venue, on_progress=progress)
                emit("phase_done", phase=phase, count=len(stores), ms=ms())

                # --- Save + diff vs previous ----------------------------
                emit("phase", phase="save", title="Saving snapshot & comparing to last run", ms=ms())
                prev = storage.latest_for(venue)
                changes = mallcore.diff_stores(prev.get("stores") if prev else None, stores) if prev else None

                utc = mallcore.now_utc_iso()
                rid = f"{mallcore.venue_id_component(venue)}__{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')}"
                method_prefix = "moa_api" if is_moa else ("mapplic_csv" if is_mapplic else "mappedin_api")
                geometry_store.save_snapshot(venue, rid, stores)
                geometry_changes = geodiff.diff_snapshots(geometry_store.previous_snapshot(venue),
                                                           geometry_store.latest_snapshot(venue))

                doc = {
                    "id": rid, "venue": venue,
                    "generated_utc": utc,
                    "generated": mallcore.to_ist_display(utc),
                    "method": f"{method_prefix}:{venue}",
                    "stores": stores, "changes": changes,
                    "geometry_changes": geometry_changes,
                }
                if changes is None:
                    progress("save", "First run for this mall — nothing to compare against yet.")
                else:
                    n = len(changes["added"]) + len(changes["removed"]) + len(changes["moved"])
                    progress("save", "No changes since the last run." if n == 0
                             else f"Found {n} change(s) since the last run.")
                if geometry_changes:
                    progress("save", f"Detected {len(geometry_changes)} footprint change(s) "
                                      "(expansions, removals, splits, etc.) — see the shape export.",
                             level="warn")

                # --- Source B: the mall's own website (trust layer) ------
                # Cross-checks the primary snapshot against a live scrape of
                # the mall's own directory page, in BOTH directions — catches
                # stores the primary source is missing AND non-store entries
                # (rides, parking, offices, etc.) the primary source over-includes.
                recon = None
                if reconcile_mod.available():
                    emit("phase", phase="reconcile", title="Cross-checking against the mall's website", ms=ms())
                    try:
                        recon = reconcile_mod.reconcile(stores, venue, on_progress=progress)
                        recon["checked_at"] = mallcore.to_ist_display(utc)
                        if recon["in_sync"]:
                            progress("reconcile", "Both sources agree — nothing missing or extra.", level="good")
                        else:
                            bits = []
                            if recon["missing_from_api"]:
                                bits.append(f"{len(recon['missing_from_api'])} on the website but not in the data")
                            if recon["extra_in_api"]:
                                bits.append(f"{len(recon['extra_in_api'])} in the data but not on the website")
                            progress("reconcile", " · ".join(bits) + ".", level="warn")
                    except reconcile_mod.BotWallError as e:
                        recon = {"blocked": True, "error": str(e)}
                        progress("reconcile", "Blocked by the website's bot check. Snapshot is still "
                                 "saved — try again later.", level="warn")
                    except Exception as e:
                        recon = {"error": str(e)}
                        progress("reconcile", f"Cross-check failed: {e}", level="warn")
                else:
                    progress("reconcile", "Cross-check not configured (no HYPERBROWSER_API_KEY) — skipped.")

                doc["reconciliation"] = recon
                storage.save_run(doc)
                emit("done", doc=doc, ms=ms())
            except Exception as e:
                emit("error", msg=str(e), ms=ms())
            finally:
                q.put(None)  # sentinel: tells the generator we're finished

        threading.Thread(target=worker, daemon=True).start()

        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"  # stop idle proxies dropping the stream
                continue
            if item is None:
                break
            event, data = item
            yield sse(event, **data)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # stop proxies (incl. Render's) buffering the stream
            "Connection": "keep-alive",
        },
    )


@app.post("/api/reconcile")
def api_reconcile():
    if not reconcile_mod.available():
        return jsonify({
            "error": "Reconciliation isn't configured on this deployment. "
                     "Set HYPERBROWSER_API_KEY to enable it."
        }), 501

    body = request.get_json(silent=True) or {}
    venue = body.get("venue") or load_venues()[0]

    # Always compare against a FRESH primary-source pull, not the last saved
    # run — this is a "right now vs right now" check, not a diff against history.
    try:
        if mallcore.is_moa_venue(venue):
            api_stores = mallcore.parse_moa_venue(venue)
        elif mallcore.is_mapplic_venue(venue):
            api_stores = mallcore.parse_mapplic_venue(venue)
        else:
            api_stores = mallcore.parse_mappedin_venue(venue)
    except Exception as e:
        return jsonify({"error": f"Primary-source pull failed for '{venue}': {e}"}), 502

    try:
        result = reconcile_mod.reconcile(api_stores, venue)
    except reconcile_mod.BotWallError as e:
        # Transient: the mall's bot challenge blocked us. Distinct from a real
        # failure — 503 + retryable so the UI can say "try again", not "broken".
        return jsonify({"error": str(e), "retryable": True}), 503
    except Exception as e:
        return jsonify({"error": f"Directory-site reconciliation check failed: {e}"}), 502

    result["checked_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(result)


def _geojson_response(doc: dict, filename: str) -> Response:
    return Response(
        json.dumps(doc),
        mimetype="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/run/<rid>/shapes.geojson")
def api_run_shapes_geojson(rid):
    """Current store footprint polygons for this run — the 'snapshot' layer,
    QGIS-ready as-is (real lon/lat for Mappedin venues, local pixel space for
    Mapplic venues — see each store's geometry_crs)."""
    doc = storage.get_run(rid)
    if doc is None:
        return jsonify({"error": "run not found"}), 404
    fc = geoexport.build_snapshot_geojson(doc.get("stores", []))
    fname = f"{mallcore.venue_id_component(doc.get('venue', 'mall'))}-{rid}-shapes.geojson"
    return _geojson_response(fc, fname)


@app.get("/api/run/<rid>/changes.geojson")
def api_run_changes_geojson(rid):
    """Geometry diff vs the previous run for this venue — expansions,
    removals, splits, merges, absorbed neighbors (see geodiff.py). This is
    the primary deliverable: QGIS can symbolize categorically straight off
    each feature's 'color' property."""
    doc = storage.get_run(rid)
    if doc is None:
        return jsonify({"error": "run not found"}), 404
    changes = doc.get("geometry_changes") or []
    fc = geoexport.build_changes_geojson(changes)
    fname = f"{mallcore.venue_id_component(doc.get('venue', 'mall'))}-{rid}-changes.geojson"
    return _geojson_response(fc, fname)


@app.get("/api/store/<store_id>/history")
def api_store_history(store_id):
    """One store's footprint area over time, read straight from the geometry
    snapshot timeline (no new storage — just filters existing snapshots)."""
    venue = request.args.get("venue") or load_venues()[0]
    history = []
    for run_id in geometry_store.list_snapshots(venue):
        snap = geometry_store.load_snapshot(venue, run_id)
        if not snap:
            continue
        for s in snap.get("stores", []):
            if s.get("id") == store_id and s.get("geometry"):
                from shapely.geometry import shape as _shape
                history.append({
                    "run_id": run_id,
                    "name": s.get("name"),
                    "area": _shape(s["geometry"]).area,
                    "geometry_crs": s.get("geometry_crs"),
                })
                break
    return jsonify({"venue": venue, "store_id": store_id, "history": history})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
