"""
Unified Mall/Store Directory Extractor

Strategy (fast/free first, general fallback second):
    1. Fetch the page via Hyperbrowser (real rendered browser, works on any site).
    2. Try known, fast, FREE, verified-accurate template parsers first
       (currently: Simon Property Group's /stores page template).
    3. If no known template matches, fall back to AI-based structured
       extraction (Hyperbrowser's `extract` job) — this works on ANY site's
       structure since it reads/understands the content semantically rather
       than pattern-matching specific HTML, but costs more per call and its
       accuracy should be spot-checked, not blindly trusted like the
       verified template parser.

Usage:
    python get_store_list_unified.py <url>

Setup:
    pip install hyperbrowser
    Set HYPERBROWSER_API_KEY in your environment.
"""

import os
import re
import io
import csv
import sys
import json
import time
import random
import zipfile
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime
from html import escape as _esc
from urllib.parse import urlparse, urljoin

from hyperbrowser import Hyperbrowser
from hyperbrowser.models import (
    StartScrapeJobParams,
    ScrapeOptions,
    StartExtractJobParams,
)

HYPERBROWSER_API_KEY = os.environ.get("HYPERBROWSER_API_KEY")

# --- Mappedin (primary path for Simon / Mappedin-powered malls) -------------
# These are the venue's PUBLIC web keys, harmless to reuse. If a call ever
# 401s they've rotated: open any mall's /map page -> DevTools -> Network ->
# filter "mappedin" -> copy the x-mappedin-key / x-mappedin-secret headers,
# or set them via the MAPPEDIN_KEY / MAPPEDIN_SECRET environment variables.
MAPPEDIN_KEY = os.environ.get("MAPPEDIN_KEY", "NTdkMDQ3MjEwMDQyZDUwMmQyMDAwMDAw")
MAPPEDIN_SECRET = os.environ.get("MAPPEDIN_SECRET", "MjUwM2M3ZjM3N2Y5NzliNWZlYmE4YzU5ZjY2NWE4Y2M=")
MAPPEDIN_API = "https://api-gateway.mappedin.com"
# Store unit codes look like A010, B260A, C100; amenity codes are pure digits
# or POLY-/LOC-/PLAY. This keeps real tenants and drops amenities.
STORE_UNIT_RE = re.compile(r"^[A-Z]\d{2,3}[A-Z]?$")

STORE_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "stores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "floor": {"type": ["string", "null"]},
                    "category": {"type": ["string", "null"]},
                    "status": {"type": ["string", "null"]},
                },
                "required": ["name"],
            },
        }
    },
    "required": ["stores"],
}

EXTRACT_PROMPT = (
    "This is a shopping mall's store directory page. Extract every store, "
    "restaurant, or tenant listed. For each one, return its name, floor/level "
    "if shown, category if shown, and status if shown (e.g. Open, Closed, "
    "Coming Soon, Relocated). Do not include navigation links, ads, or "
    "unrelated page elements — only actual tenant/store listings."
)


# ---------------------------------------------------------------------------
# KNOWN TEMPLATE PARSERS — fast, free, verified accurate. Add more here as
# you build/verify them for other mall operators (Westfield, Brookfield, etc.)
# ---------------------------------------------------------------------------

def is_simon_property_page(content: str, url: str) -> bool:
    return "premiumoutlets.com" in url or "simon.com" in url or "Simon Property" in content


def parse_simon_stores_page(content: str) -> list[dict]:
    blocks = re.split(r"(?=\[!\[)", content)
    stores = {}
    slug_re = re.compile(r"stores/([a-z0-9\-]+)[\s\"/]")
    name_re = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
    floor_re = re.compile(r"\[([^\]]+?)\]\(https://[^)]*?/map/#/profile\?location=")
    center_map_re = re.compile(r"\[VIEW ON CENTER MAP\]\(https://[^)]*?/map\b")

    for block in blocks:
        slug_m = slug_re.search(block)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        if slug in stores:
            continue

        name_m = name_re.search(block)
        if not name_m:
            continue
        raw_name_segment = name_m.group(1)
        name = re.sub(r"\\+|\s+", " ", raw_name_segment).strip()
        name = re.sub(r"\s*Coming Soon\s*$", "", name, flags=re.IGNORECASE).strip()

        floor_m = floor_re.search(block)
        center_map_m = center_map_re.search(block)
        floor = floor_m.group(1).strip() if floor_m else None

        if floor_m is None and center_map_m is None:
            continue

        link_end = (floor_m or center_map_m).end()
        search_window = block[:link_end]
        is_coming_soon = bool(re.search(r"coming soon", search_window, re.IGNORECASE))
        status = "Coming Soon" if is_coming_soon else "Open"

        stores[slug] = {"name": name, "slug": slug, "floor": floor,
                         "category": None, "status": status}

    return sorted(stores.values(), key=lambda x: x["name"])


def parse_simon_store_detail_page(content: str) -> dict:
    """
    Parses an individual Simon store detail page (e.g. .../stores/finish-line)
    to extract the unit/location code — NOT available on the /stores
    directory listing, only on each store's own page.

    Confirmed field: "Location in Outlet:" followed by a code like "A10"
    on the next line.
    """
    result = {"location_in_outlet": None, "best_entrance": None}

    loc_m = re.search(
        r"Location in Outlet:\s*\n+\s*([A-Za-z0-9\-]+)",
        content,
    )
    if loc_m:
        result["location_in_outlet"] = loc_m.group(1).strip()

    entrance_m = re.search(
        r"Best Entrance:\s*\n+\s*([^\n]+)",
        content,
    )
    if entrance_m:
        result["best_entrance"] = entrance_m.group(1).strip()

    return result


KNOWN_TEMPLATES = [
    (is_simon_property_page, parse_simon_stores_page, "Simon Property Group"),
    # Add more (detector_fn, parser_fn, label) tuples here as you build them
    # for other mall operators/site templates.
]


# ---------------------------------------------------------------------------
# PRIMARY PATH — Mappedin venue API (Simon & other Mappedin-powered malls)
#
# Pulls the ENTIRE directory *and* unit/location codes in ~2 requests, with
# NO browser, NO Hyperbrowser credits, and NO "Press & Hold" challenge — the
# Mappedin endpoints are unprotected. This obsoletes the per-store scraping
# for any mall whose map is powered by Mappedin. Falls back gracefully (raises)
# so the caller can drop to the scraper if a site isn't Mappedin-backed.
# ---------------------------------------------------------------------------

def _mi_headers() -> dict:
    return {
        "x-mappedin-key": MAPPEDIN_KEY,
        "x-mappedin-secret": MAPPEDIN_SECRET,
        "User-Agent": "Mozilla/5.0",
    }


def _http_get(url: str, headers: dict | None = None, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def mappedin_venue_from_url(url: str) -> str | None:
    """
    Derive the Mappedin venue slug from a Simon outlet URL.
      https://www.premiumoutlets.com/outlet/albertville/stores -> simon-albertville
    Returns None if it can't be determined (caller can still pass one explicitly).
    """
    m = re.search(r"/outlet/([a-z0-9\-]+)", url, re.IGNORECASE)
    if m:
        return f"simon-{m.group(1).lower()}"
    return None


def _status_from_states(states: list[dict] | None) -> str:
    """Mappedin's location.states carries e.g. [{"type": "coming-soon", ...}]
    when a store isn't open yet. Empty/absent -> a normal open store. Maps
    known types to the label the old Simon-page scraper used ("Coming Soon"
    / "Open") and falls back to a readable label for any type we haven't
    seen yet, so a new status type shows up as data instead of silently
    being dropped to "Open"."""
    if not states:
        return "Open"
    t = (states[0].get("type") or "").strip()
    return {"coming-soon": "Coming Soon"}.get(t) or (t.replace("-", " ").title() or "Open")


def parse_mappedin_venue(venue: str) -> list[dict]:
    """
    Fetch a full store directory with unit codes from the Mappedin API.

    Two calls:
      1. /public/1/location/{venue}   -> authoritative list of real stores
      2. /exports/mvf2/1/bundle       -> signed zip whose space/*.geojson
                                         carries each store's unit code
                                         (externalId) joined to its name.

    Returns a list of store dicts (name, slug, floor, location_in_outlet,
    category, status). The unit code is stored in BOTH `location_in_outlet`
    (to match the Simon detail-page parser) and `floor` (for display). Raises
    on any failure so the caller can fall back to the scraper.
    """
    headers = _mi_headers()

    # 1. real store names (type == "store"); amenities/parking are excluded here.
    # `states` carries the coming-soon flag Simon's /stores page also shows.
    loc_url = f"{MAPPEDIN_API}/public/1/location/{venue}?fields=id,externalId,name,type,states"
    locations = json.loads(_http_get(loc_url, headers).decode("utf-8"))
    status_by_name: dict[str, str] = {
        loc.get("name"): _status_from_states(loc.get("states"))
        for loc in locations
        if loc.get("type") == "store" and loc.get("name")
    }
    store_names = set(status_by_name)
    if not store_names:
        raise RuntimeError(f"Mappedin returned no stores for venue '{venue}'")

    # 2. venue bundle -> follow the signed CDN zip url -> read space geojson
    bundle_meta = json.loads(
        _http_get(f"{MAPPEDIN_API}/exports/mvf2/1/bundle?venue={venue}&version=1.0.0", headers).decode("utf-8")
    )
    zip_url = (
        (bundle_meta.get("perspectives", {}).get("Website", {}) or {}).get("url")
        or bundle_meta.get("url")
    )
    if not zip_url:
        raise RuntimeError("Mappedin bundle response contained no zip URL")

    zip_bytes = _http_get(zip_url, headers, timeout=120)
    space_features: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.startswith("space/") and name.endswith(".geojson"):
                data = json.loads(zf.read(name).decode("utf-8"))
                space_features.extend(data.get("features", []))

    # join unit code -> store name (only store-shaped codes, first wins)
    unit_by_name: dict[str, str] = {}
    for feat in space_features:
        props = feat.get("properties", {}) or {}
        name = (props.get("details", {}) or {}).get("name")
        ext = props.get("externalId")
        if (
            name in store_names
            and ext
            and STORE_UNIT_RE.match(ext)
            and name not in unit_by_name
        ):
            unit_by_name[name] = ext

    stores = []
    for name in sorted(store_names):
        unit = unit_by_name.get(name)
        stores.append({
            "name": name,
            "slug": None,
            "floor": unit,
            "location_in_outlet": unit,
            "category": None,
            "status": status_by_name.get(name, "Open"),
        })
    return stores


# ---------------------------------------------------------------------------
# PRIMARY PATH — Mapplic CSV directory (Friendly Center & other Mapplic-
# powered malls). Mapplic embeds a small `data-json="https://mapplic.com/
# getMapData?id=..."` attribute on the map container; that JSON response in
# turn references a `settings.csv` asset URL holding the ENTIRE tenant
# directory as a flat CSV. No browser, no bot challenge, no AI extraction.
# ---------------------------------------------------------------------------

def mapplic_getmapdata_url_from_page(url: str) -> str | None:
    """Fetch a page's raw HTML and look for its Mapplic map embed URL.
    Returns None if the page isn't Mapplic-powered (caller falls back)."""
    try:
        html = _http_get(url, headers={"User-Agent": "Mozilla/5.0"}).decode("utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r'(https://mapplic\.com/getMapData\?id=[A-Za-z0-9_-]+)', html)
    return m.group(1) if m else None


def parse_mapplic_stores(getmapdata_url: str) -> list[dict]:
    """
    Columns seen (Friendly Center): "Property Name","id","title","link",
    "about","desc","image","layer","style","disable","hide","group","sample"
    - disable=="TRUE" or title=="VACANT" -> not a real tenant, skip.
    - group is a comma-separated tag list; doubles as category and carries
      status tags like "Coming Soon" / "Now Open" / "Temporarily Closed".
    - id is the map's own unit code (e.g. "u103").
    """
    map_data = json.loads(_http_get(getmapdata_url, headers={"User-Agent": "Mozilla/5.0"}))
    csv_url = (map_data.get("settings") or {}).get("csv")
    if not csv_url:
        raise RuntimeError("Mapplic map data has no settings.csv URL")

    csv_bytes = _http_get(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    rows = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))

    status_tags = ("Coming Soon", "Now Open", "Temporarily Closed")

    stores = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title or title.upper() == "VACANT":
            continue
        if (row.get("disable") or "").strip().upper() == "TRUE":
            continue

        tags = [t.strip() for t in (row.get("group") or "").split(",") if t.strip()]
        status = next((t for t in tags if t in status_tags), "Open")
        category = ", ".join(t for t in tags if t not in status_tags) or None

        stores.append({
            "name": title,
            "slug": (row.get("link") or "").strip() or None,
            "floor": None,
            "location_in_outlet": (row.get("id") or "").strip() or None,
            "category": category,
            "status": status,
        })

    return sorted(stores, key=lambda x: x["name"])


# ---------------------------------------------------------------------------
# GENERAL FALLBACK — AI extraction, works on any site, any structure
# ---------------------------------------------------------------------------

def extract_via_ai(client: Hyperbrowser, url: str) -> list[dict]:
    result = client.extract.start_and_wait(
        StartExtractJobParams(
            urls=[url],
            prompt=EXTRACT_PROMPT,
            schema=STORE_LIST_SCHEMA,
            session_options={"use_stealth": True},
        )
    )
    if result.status != "completed":
        raise RuntimeError(f"Extract job failed: status={result.status}, error={getattr(result, 'error', None)}")
    data = result.data
    stores = data.get("stores", []) if isinstance(data, dict) else []
    for s in stores:
        s.setdefault("floor", None)
        s.setdefault("category", None)
        s.setdefault("status", None)
    return stores


# ---------------------------------------------------------------------------
# DATA-FEED PROBE — find the JSON/API endpoint that powers the map so you can
# pull ALL store locations in one request instead of fetching each store page
# (which is what trips the "Press & Hold" bot challenge). This does NOT defeat
# the challenge — it looks for a data source that isn't protected in the first
# place, which is the reliable, low-friction path.
# ---------------------------------------------------------------------------

# URL patterns that tend to be data endpoints rather than page/asset links.
_FEED_HINT_RE = re.compile(
    r"""(?xi)
    ( /api/ | /graphql | \.json(?:[?"']|$)
    | /map(?:s)?/ | location | store | tenant | directory | feed
    | mappedin | cdn\.jsdelivr | \.arcgis\. )
    """
)
# Assets we never care about even if they match a hint above.
_FEED_NOISE_RE = re.compile(
    r"(?i)\.(?:png|jpe?g|gif|svg|webp|ico|css|woff2?|ttf|eot|mp4|webm)(?:[?#]|$)"
)
# Grab things that look like URLs inside HTML/JS: src/href attrs, fetch(),
# quoted absolute/relative paths, and JSON string values.
_URL_TOKEN_RE = re.compile(
    r"""(?xi)
    (?:https?://[^\s"'()<>]+)          # absolute URLs
    | (?:["'])(/[^\s"'()<>]{3,})(?:["'])  # quoted root-relative paths
    """
)


def probe_data_feed(client: Hyperbrowser, url: str) -> list[dict]:
    """
    Fetch the page (and, if present, its /map page) as raw HTML and surface
    candidate data-feed URLs, ranked by how likely they are to be the store
    location source. Returns a list of {"url", "score", "why"} dicts.

    NOTE: This scans the served HTML/inline JS for endpoint references. It will
    catch feeds referenced in the page source; it cannot see XHR calls made
    only after runtime interaction. For those, open DevTools -> Network -> XHR
    on the /map page manually. This helper gets you 80% of the way for free.
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Pages worth scanning: the directory page itself, and the map page which
    # is almost always backed by a location data feed.
    base = url.rstrip("/").rsplit("/stores", 1)[0]
    pages_to_scan = [url]
    for candidate in (f"{base}/map", f"{base}/map/", f"{base}/directory"):
        if candidate not in pages_to_scan:
            pages_to_scan.append(candidate)

    found: dict[str, dict] = {}

    for page in pages_to_scan:
        print(f"[PROBE] Fetching HTML of {page} ...")
        try:
            res = client.scrape.start_and_wait(
                StartScrapeJobParams(
                    url=page,
                    scrape_options=ScrapeOptions(formats=["html"]),
                    session_options={"use_stealth": True},
                )
            )
        except Exception as e:
            print(f"[PROBE]   skipped ({e})")
            continue

        if res.status != "completed":
            print(f"[PROBE]   skipped (status={res.status})")
            continue

        html = (getattr(res.data, "html", None) or "") if res.data else ""
        if not html:
            print("[PROBE]   no HTML returned")
            continue

        for m in _URL_TOKEN_RE.finditer(html):
            raw = m.group(0).strip("\"'") if m.group(0).startswith(("http", "\"", "'")) else m.group(1)
            if not raw:
                continue
            candidate = raw if raw.startswith("http") else urljoin(origin, raw)

            if _FEED_NOISE_RE.search(candidate):
                continue
            if not _FEED_HINT_RE.search(candidate):
                continue
            if candidate in found:
                continue

            # Score by how many independent signals point to "data feed".
            score = 0
            why = []
            for signal, pts, label in [
                (r"\.json", 3, "returns .json"),
                (r"/api/", 3, "under /api/"),
                (r"/graphql", 3, "graphql endpoint"),
                (r"location", 2, "mentions 'location'"),
                (r"tenant|store|directory", 2, "mentions store/tenant"),
                (r"map", 1, "map-related"),
                (r"mappedin|\.arcgis\.", 2, "known map vendor"),
            ]:
                if re.search(signal, candidate, re.I):
                    score += pts
                    why.append(label)
            found[candidate] = {"url": candidate, "score": score, "why": ", ".join(why)}

    ranked = sorted(found.values(), key=lambda d: d["score"], reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# CHANGE DETECTION + HTML REPORT
# On each run we diff the fresh results against the previous run's saved JSON,
# so the report can show what changed on the map: stores added, removed, or
# relocated (unit code changed). Then we render a self-contained HTML file
# (inline CSS, works offline, no dependencies).
# ---------------------------------------------------------------------------

def _store_key(s: dict) -> str:
    # slug is stable on the scraper path; name is the stable key on Mappedin.
    return s.get("slug") or s.get("name") or ""


def _unit_of(s: dict):
    return s.get("location_in_outlet") or s.get("floor")


def diff_stores(old: list[dict] | None, new: list[dict]) -> dict:
    """Compare two store lists -> {added, removed, moved}. `moved` = same store,
    different unit code (a relocation on the map)."""
    oldm = {_store_key(s): s for s in (old or [])}
    newm = {_store_key(s): s for s in (new or [])}

    added = [newm[k] for k in newm if k not in oldm]
    removed = [oldm[k] for k in oldm if k not in newm]
    moved = []
    for k in newm:
        if k in oldm:
            o_u, n_u = _unit_of(oldm[k]), _unit_of(newm[k])
            if (o_u or None) != (n_u or None):
                moved.append({"name": newm[k].get("name"), "from": o_u, "to": n_u})

    key = lambda s: (s.get("name") or "").lower()
    return {
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "moved": sorted(moved, key=lambda m: (m["name"] or "").lower()),
    }


def write_html_report(stores: list[dict], changes: dict | None, meta: dict, path: Path) -> None:
    """Render a self-contained, theme-aware HTML report of the current stores
    plus a 'changes since last run' section."""
    title = meta.get("title", "Store Directory")
    generated = meta.get("generated", "")
    method = meta.get("method", "")
    prev = meta.get("prev_generated")

    def row_cells(name, unit, status):
        return (f"<td>{_esc(str(name or ''))}</td>"
                f"<td class='u'>{_esc(str(unit or '—'))}</td>"
                f"<td>{_esc(str(status or ''))}</td>")

    # --- changes block ---
    if changes is None:
        changes_html = ("<p class='muted'>First recorded run — no previous snapshot "
                        "to compare against yet. Re-run later to see changes.</p>")
    else:
        added, removed, moved = changes["added"], changes["removed"], changes["moved"]
        total = len(added) + len(removed) + len(moved)
        if total == 0:
            changes_html = "<p class='ok'>✓ No changes since the last run.</p>"
            if prev:
                changes_html += f"<p class='muted'>Compared against {_esc(prev)}.</p>"
        else:
            parts = [f"<p class='muted'>Compared against {_esc(prev or 'previous run')}.</p>"]
            if added:
                parts.append("<h3 class='added'>➕ New ({0})</h3><ul>".format(len(added)))
                parts += [f"<li>{_esc(s.get('name') or '')} "
                          f"<span class='u'>{_esc(str(_unit_of(s) or '—'))}</span></li>" for s in added]
                parts.append("</ul>")
            if removed:
                parts.append("<h3 class='removed'>➖ Gone ({0})</h3><ul>".format(len(removed)))
                parts += [f"<li>{_esc(s.get('name') or '')} "
                          f"<span class='u'>{_esc(str(_unit_of(s) or '—'))}</span></li>" for s in removed]
                parts.append("</ul>")
            if moved:
                parts.append("<h3 class='moved'>↔ Relocated ({0})</h3><ul>".format(len(moved)))
                parts += [f"<li>{_esc(m['name'] or '')}: "
                          f"<span class='u'>{_esc(str(m['from'] or '—'))}</span> → "
                          f"<span class='u'>{_esc(str(m['to'] or '—'))}</span></li>" for m in moved]
                parts.append("</ul>")
            changes_html = "".join(parts)

    # --- store table ---
    rows = "".join(
        f"<tr>{row_cells(s.get('name'), _unit_of(s), s.get('status'))}</tr>"
        for s in sorted(stores, key=lambda s: (s.get("name") or "").lower())
    )

    html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 2rem 1rem; background:#f6f7f9; color:#1c1f23; }}
.wrap {{ max-width: 820px; margin: 0 auto; }}
h1 {{ font-size: 1.5rem; margin:0 0 .25rem; }}
.sub {{ color:#6b7280; font-size:.85rem; margin:0 0 1.5rem; }}
.card {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px;
        padding:1.25rem 1.5rem; margin-bottom:1.25rem; }}
h2 {{ font-size:1rem; text-transform:uppercase; letter-spacing:.04em;
      color:#6b7280; margin:0 0 .75rem; }}
h3 {{ font-size:.95rem; margin:1rem 0 .35rem; }}
ul {{ margin:.25rem 0 .5rem; padding-left:1.25rem; }}
li {{ margin:.15rem 0; }}
table {{ width:100%; border-collapse:collapse; }}
th, td {{ text-align:left; padding:.5rem .6rem; border-bottom:1px solid #eef0f2; }}
th {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; color:#6b7280; }}
td.u {{ font-variant-numeric:tabular-nums; font-family:ui-monospace, SFMono-Regular, Menlo, monospace;
        color:#374151; white-space:nowrap; }}
.muted {{ color:#6b7280; font-size:.85rem; }}
.ok {{ color:#15803d; font-weight:600; }}
.added, .u {{ }}
.added {{ color:#15803d; }} .removed {{ color:#b91c1c; }} .moved {{ color:#b45309; }}
.pill {{ display:inline-block; background:#eef2ff; color:#3730a3; border-radius:999px;
         padding:.1rem .6rem; font-size:.75rem; margin-left:.4rem; }}
@media (prefers-color-scheme: dark) {{
  body {{ background:#0f1115; color:#e5e7eb; }}
  .card {{ background:#161a20; border-color:#262b33; }}
  th, td {{ border-color:#242932; }}
  td.u {{ color:#c7ccd3; }}
  .pill {{ background:#1e2338; color:#a5b4fc; }}
}}
</style></head>
<body><div class="wrap">
  <h1>{_esc(title)}<span class="pill">{len(stores)} stores</span></h1>
  <p class="sub">Generated {_esc(generated)} &middot; source: {_esc(method)}</p>

  <div class="card">
    <h2>Changes since last run</h2>
    {changes_html}
  </div>

  <div class="card">
    <h2>Current directory</h2>
    <table>
      <thead><tr><th>Store</th><th>Unit</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div></body></html>"""
    path.write_text(html_doc, encoding="utf-8")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Mall/store directory URL")
    parser.add_argument(
        "--with-unit-numbers",
        action="store_true",
        help="Also fetch each store's individual page to get its unit/location code (slower, one extra request per store)",
    )
    parser.add_argument(
        "--debug-one-store",
        metavar="SLUG",
        help="Fetch just ONE store's detail page by slug (e.g. 'finish-line') and save its raw content to a file for inspection, instead of running the full batch.",
    )
    parser.add_argument(
        "--probe-feed",
        action="store_true",
        help="Scan the directory/map page HTML for candidate JSON/API data-feed URLs "
             "that may return ALL store locations in one request (avoids per-store "
             "fetching and the bot challenge). Prints ranked candidates and exits.",
    )
    parser.add_argument(
        "--mappedin-venue",
        metavar="SLUG",
        help="Force the Mappedin venue slug (e.g. 'simon-albertville') instead of "
             "deriving it from the URL. Use for non-Simon Mappedin malls.",
    )
    parser.add_argument(
        "--no-mappedin",
        action="store_true",
        help="Skip the fast Mappedin API path and go straight to the browser scraper "
             "(useful for testing the fallback).",
    )
    parser.add_argument(
        "--no-mapplic",
        action="store_true",
        help="Skip the fast Mapplic CSV path and go straight to the browser scraper "
             "(useful for testing the fallback).",
    )
    args = parser.parse_args()

    # Hyperbrowser is only needed for the scraper/AI paths, not the Mappedin API
    # path — so build the client lazily and only require the key when we use it.
    _client_holder: dict[str, Hyperbrowser] = {}

    def get_client() -> Hyperbrowser:
        if "client" not in _client_holder:
            if not HYPERBROWSER_API_KEY:
                sys.exit("[ERROR] This path needs Hyperbrowser. Set the "
                         "HYPERBROWSER_API_KEY environment variable, or use the "
                         "Mappedin path (a Simon URL or --mappedin-venue).")
            _client_holder["client"] = Hyperbrowser(api_key=HYPERBROWSER_API_KEY)
        return _client_holder["client"]

    if args.probe_feed:
        client = get_client()
        candidates = probe_data_feed(client, args.url)
        if not candidates:
            print("\n[PROBE] No obvious data-feed URLs found in the served HTML.")
            print("        The feed is likely loaded via a runtime XHR call. Next step:")
            print("        open the /map page in Chrome, DevTools -> Network -> Fetch/XHR,")
            print("        and watch for a JSON request as the map loads.")
            sys.exit(0)

        print(f"\n[PROBE] {len(candidates)} candidate data-feed URL(s), best first:\n")
        for c in candidates[:25]:
            print(f"  [score {c['score']:>2}] {c['url']}")
            if c["why"]:
                print(f"             ({c['why']})")

        out_path = Path("feed_candidates.json")
        out_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
        print(f"\nSaved full list to {out_path.resolve()}")
        print("Open the top candidate(s) in a browser/curl to confirm they return")
        print("store+location JSON. If one does, you can fetch ALL units in ONE request.")
        sys.exit(0)

    if args.debug_one_store:
        client = get_client()
        base_url = args.url.rstrip("/").rsplit("/stores", 1)[0]
        detail_url = f"{base_url}/stores/{args.debug_one_store}"
        print(f"Fetching single store page for debugging: {detail_url}")
        result = client.scrape.start_and_wait(
            StartScrapeJobParams(
                url=detail_url,
                scrape_options=ScrapeOptions(formats=["markdown"]),
                session_options={"use_stealth": True},
            )
        )
        if result.status != "completed":
            print(f"[ERROR] Fetch failed: {result.status}")
            sys.exit(1)
        raw_content = result.data.markdown or ""
        out_path = Path(f"debug_{args.debug_one_store}.md")
        out_path.write_text(raw_content, encoding="utf-8")
        print(f"Saved raw content to {out_path.resolve()} ({len(raw_content)} chars)")
        print("\nUpload this file so the parser regex can be fixed against the real content.")
        sys.exit(0)

    # -----------------------------------------------------------------------
    # PRIMARY: Mappedin API. Fast, free, includes unit codes, no bot challenge.
    # -----------------------------------------------------------------------
    stores = None
    method = None
    matched_template = None  # set only when the scraper/template path is used

    venue = args.mappedin_venue or (None if args.no_mappedin else mappedin_venue_from_url(args.url))
    if venue and not args.no_mappedin:
        print(f"[INFO] Trying Mappedin API for venue '{venue}' (fast, free, no browser)...")
        try:
            stores = parse_mappedin_venue(venue)
            method = f"mappedin_api:{venue}"
            with_units = sum(1 for s in stores if s.get("location_in_outlet"))
            print(f"[OK] Mappedin returned {len(stores)} stores "
                  f"({with_units} with unit codes) — no per-store fetching needed.\n")
        except Exception as e:
            print(f"[WARN] Mappedin path failed ({e}); falling back to the browser scraper.\n")
            stores = None

    if stores is None and not args.no_mapplic:
        mapplic_url = mapplic_getmapdata_url_from_page(args.url)
        if mapplic_url:
            print(f"[INFO] Detected a Mapplic-powered map ({mapplic_url}) — "
                  f"trying its CSV directory feed (fast, free, no browser)...")
            try:
                stores = parse_mapplic_stores(mapplic_url)
                method = "mapplic_csv"
                print(f"[OK] Mapplic CSV returned {len(stores)} stores — no browser/AI needed.\n")
            except Exception as e:
                print(f"[WARN] Mapplic path failed ({e}); falling back to the browser scraper.\n")
                stores = None

    # -----------------------------------------------------------------------
    # FALLBACK: Hyperbrowser scrape -> known template -> AI extraction.
    # -----------------------------------------------------------------------
    if stores is None:
        client = get_client()
        print(f"Fetching {args.url} ...")
        scrape_result = client.scrape.start_and_wait(
            StartScrapeJobParams(
                url=args.url,
                scrape_options=ScrapeOptions(formats=["markdown"]),
                session_options={"use_stealth": True},
            )
        )
        if scrape_result.status != "completed":
            print(f"[ERROR] Scrape failed: {scrape_result.status}")
            sys.exit(1)

        content = scrape_result.data.markdown or ""
        print(f"Fetched {len(content)} characters.\n")

        for detector, template_parser, label in KNOWN_TEMPLATES:
            if detector(content, args.url):
                matched_template = (template_parser, label)
                break

        if matched_template:
            template_parser, label = matched_template
            print(f"[INFO] Recognized known template: {label} — using fast, verified parser.\n")
            stores = template_parser(content)
            method = f"known_template:{label}"
        else:
            print("[INFO] No known template matched — falling back to AI extraction.")
            print("[NOTE] AI extraction is more general but should be spot-checked,")
            print("       not assumed 100% accurate like a verified template parser.\n")
            stores = extract_via_ai(client, args.url)
            method = "ai_extraction"

    print(f"Extracted {len(stores)} stores (method: {method}):\n")
    for s in stores:
        print(f"  {s.get('name'):40s} | unit/floor={str(s.get('floor')):16s} | "
              f"category={str(s.get('category')):20s} | status={s.get('status')}")

    out_path = Path(re.sub(r"[^a-zA-Z0-9]+", "_", args.url).strip("_") + ".stores.json")

    # --- Batch fetch each store's individual page for unit number, if requested ---
    if args.with_unit_numbers and method and (method.startswith("mappedin_api") or method == "mapplic_csv"):
        print("\n[INFO] --with-unit-numbers is unnecessary here: this path "
              "already returned unit codes. Skipping per-store fetching.")
    elif args.with_unit_numbers and matched_template:
        base_url = args.url.rstrip("/").rsplit("/stores", 1)[0]

        # --- Checkpoint/resume ---------------------------------------------
        # Progress is saved to a sidecar file keyed by slug after every
        # successful fetch. A re-run picks up exactly where it left off, so a
        # failure at store 150/200 never re-fetches the first 149. Safe to
        # Ctrl-C and resume. Delete the .partial.json to force a clean re-run.
        checkpoint_path = Path(str(out_path) + ".partial.json")
        checkpoint: dict[str, dict] = {}
        if checkpoint_path.exists():
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                print(f"\n[RESUME] Loaded checkpoint {checkpoint_path.name} "
                      f"({len(checkpoint)} store(s) already done).")
            except (json.JSONDecodeError, OSError) as e:
                print(f"\n[WARN] Could not read checkpoint {checkpoint_path.name} ({e}); starting fresh.")
                checkpoint = {}

        def save_checkpoint() -> None:
            tmp = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
            tmp.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
            tmp.replace(checkpoint_path)  # atomic on same filesystem

        pending = [s for s in stores if s.get("slug") and s["slug"] not in checkpoint]
        print(f"\n[INFO] {len(stores)} store(s) total; {len(checkpoint)} cached, "
              f"{len(pending)} to fetch.")
        print("[NOTE] This makes one request per store — slower and costs more credits.")
        print("[NOTE] Some fetches may hit bot-protection intermittently; each gets up to 3 attempts.\n")

        failed_stores = []

        for i, s in enumerate(stores):
            slug = s.get("slug")
            if not slug:
                continue

            # Already done in a previous run (or earlier this run) — reuse it.
            if slug in checkpoint:
                s["location_in_outlet"] = checkpoint[slug].get("location_in_outlet")
                s["best_entrance"] = checkpoint[slug].get("best_entrance")
                continue

            detail_url = f"{base_url}/stores/{slug}"

            success = False
            for attempt in range(1, 4):  # up to 3 attempts per store
                try:
                    detail_result = client.scrape.start_and_wait(
                        StartScrapeJobParams(
                            url=detail_url,
                            scrape_options=ScrapeOptions(formats=["markdown"]),
                            session_options={"use_stealth": True},
                        )
                    )
                    if detail_result.status == "completed":
                        detail_content = detail_result.data.markdown or ""

                        # Detect the "Press & Hold" bot-challenge page specifically —
                        # this looks like success (status=completed) but contains no
                        # real store data, so treat it as a failure and retry.
                        if "press & hold" in detail_content.lower() or "confirm you are" in detail_content.lower():
                            raise RuntimeError("bot-challenge page returned instead of real content")

                        detail_info = parse_simon_store_detail_page(detail_content)
                        s["location_in_outlet"] = detail_info["location_in_outlet"]
                        s["best_entrance"] = detail_info["best_entrance"]

                        # Persist immediately so a later crash never loses this.
                        checkpoint[slug] = {
                            "location_in_outlet": detail_info["location_in_outlet"],
                            "best_entrance": detail_info["best_entrance"],
                        }
                        save_checkpoint()

                        attempt_note = f" (attempt {attempt})" if attempt > 1 else ""
                        print(f"  [{i+1}/{len(stores)}] {s['name']:35s} -> unit: {detail_info['location_in_outlet']}{attempt_note}")
                        success = True
                        break
                    else:
                        raise RuntimeError(f"scrape status={detail_result.status}")

                except Exception as e:
                    if attempt < 3:
                        # Randomized backoff before retrying — small delay helps
                        # avoid looking like a rapid-fire bot pattern.
                        delay = random.uniform(4, 9)
                        print(f"  [{i+1}/{len(stores)}] {s['name']:35s} -> attempt {attempt} failed ({e}), retrying in {delay:.1f}s...")
                        time.sleep(delay)
                    else:
                        print(f"  [{i+1}/{len(stores)}] {s['name']:35s} -> FAILED after 3 attempts ({e})")
                        s["location_in_outlet"] = None
                        s["best_entrance"] = None
                        failed_stores.append(s["name"])

            # Randomized delay between different stores too, not just retries —
            # reduces the "rapid sequential requests" pattern that seems to
            # trigger bot-protection more often.
            if success:
                time.sleep(random.uniform(2, 5))

        if failed_stores:
            print(f"\n[WARNING] {len(failed_stores)} store(s) never got a unit number after 3 attempts each:")
            for name in failed_stores:
                print(f"    - {name}")
            print("Re-run the same command to retry ONLY these — completed stores are cached")
            print(f"in {checkpoint_path.name} and will be skipped.")
        else:
            print(f"\n[OK] All {len(stores)} stores got unit numbers successfully.")
            # Clean run finished — remove the checkpoint so the next run starts fresh.
            try:
                checkpoint_path.unlink(missing_ok=True)
            except OSError:
                pass

    elif args.with_unit_numbers and not matched_template:
        print("\n[WARN] --with-unit-numbers is only supported on known-template sites")
        print("       (it needs per-store slugs). This site used AI extraction, which")
        print("       does not produce slugs, so unit numbers were skipped.")

    # --- change detection vs the previous run + HTML report ------------------
    prev_stores = None
    prev_generated = None
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            prev_stores = prev.get("stores")
            prev_generated = prev.get("generated")
        except (json.JSONDecodeError, OSError):
            prev_stores = None

    changes = diff_stores(prev_stores, stores) if prev_stores is not None else None
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Human-readable title: venue slug if we have it, else the host.
    title = venue or urlparse(args.url).netloc or "Store Directory"

    out_path.write_text(json.dumps({
        "method": method,
        "generated": generated,
        "stores": stores,
        "changes": changes,
    }, indent=2), encoding="utf-8")

    html_path = out_path.with_suffix(".html")
    write_html_report(
        stores, changes,
        {"title": title, "generated": generated, "method": method,
         "prev_generated": prev_generated},
        html_path,
    )

    # console summary of changes
    if changes is None:
        print("\n[REPORT] First recorded run — no previous snapshot to diff against.")
    else:
        n = len(changes["added"]) + len(changes["removed"]) + len(changes["moved"])
        if n == 0:
            print("\n[REPORT] No changes since the last run.")
        else:
            print(f"\n[REPORT] {len(changes['added'])} new, "
                  f"{len(changes['removed'])} gone, {len(changes['moved'])} relocated "
                  f"since {prev_generated or 'previous run'}.")

    print(f"\nSaved JSON  -> {out_path.resolve()}")
    print(f"Saved HTML  -> {html_path.resolve()}")


if __name__ == "__main__":
    main()
