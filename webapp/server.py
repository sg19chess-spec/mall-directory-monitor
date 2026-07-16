"""
Mall Directory dashboard — Flask backend.

Endpoints:
  GET  /                 -> the dashboard (static/index.html)
  GET  /api/venues       -> configured venue slugs (venues.json)
  GET  /api/runs?venue=  -> run history (newest first), light metadata
  GET  /api/run/<id>     -> one full run snapshot
  POST /api/run {venue}  -> scrape now, diff vs previous, save snapshot, return it

Run history is persisted via storage.py: Postgres when DATABASE_URL is set
(Render), else local JSON files (dev). That history is the log — nothing is
overwritten, so "Previous runs" browses the full timeline.

Local:   pip install -r requirements.txt ; python server.py -> http://127.0.0.1:5000
Render:  gunicorn server:app --bind 0.0.0.0:$PORT   (see render.yaml)
"""

import os
import json
import pathlib
import datetime
from flask import Flask, request, jsonify, send_from_directory

import mallcore
import storage

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
    return jsonify(load_venues())


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "storage": storage.BACKEND})


@app.get("/api/runs")
def api_runs():
    venue = request.args.get("venue")
    metas = []
    for d in storage.list_runs(venue):
        ch = d.get("changes") or {}
        metas.append({
            "id": d.get("id"),
            "venue": d.get("venue"),
            "generated": d.get("generated"),
            "count": len(d.get("stores", [])),
            "added": len(ch.get("added", [])),
            "removed": len(ch.get("removed", [])),
            "moved": len(ch.get("moved", [])),
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
    try:
        stores = mallcore.parse_mappedin_venue(venue)
    except Exception as e:
        return jsonify({"error": f"Scrape failed for '{venue}': {e}"}), 502

    prev = storage.latest_for(venue)
    changes = mallcore.diff_stores(prev.get("stores") if prev else None, stores) if prev else None

    now = datetime.datetime.now()
    rid = f"{venue}__{now.strftime('%Y%m%d-%H%M%S')}"
    doc = {
        "id": rid,
        "venue": venue,
        "generated": now.strftime("%Y-%m-%d %H:%M:%S"),
        "method": f"mappedin_api:{venue}",
        "stores": stores,
        "changes": changes,
    }
    storage.save_run(doc)
    return jsonify(doc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
