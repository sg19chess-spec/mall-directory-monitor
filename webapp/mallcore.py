"""
mallcore — standalone, stdlib-only core for the mall dashboard.

Deliberately independent of Hyperbrowser so the website can be hosted
anywhere Python runs, with no scraping-vendor dependency. It covers the
Mappedin primary path (directory + unit codes) and the run-to-run diff.

If you add non-Mappedin malls to the site later, wire the Hyperbrowser
fallback in server.py — keep this module dependency-free.
"""

import os
import re
import io
import csv
import json
import math
import hashlib
import zipfile
import xml.etree.ElementTree as ET
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from shapely.geometry import shape as _shapely_shape
from shapely import wkb as _shapely_wkb

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - tzdata missing
    IST = None


def now_utc_iso() -> str:
    """Machine-readable, unambiguous. What we store."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_ist_display(utc_iso: str | None) -> str:
    """Human-readable IST. What we show. Storing UTC + rendering IST keeps the
    data portable (Render runs in UTC) while the UI stays local to the user."""
    if not utc_iso:
        return "—"
    try:
        dt = datetime.fromisoformat(utc_iso)
    except ValueError:
        return utc_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if IST:
        dt = dt.astimezone(IST)
    return dt.strftime("%d %b %Y, %I:%M %p IST")


def _prettify_domain(netloc: str) -> str:
    """'friendlycenter.com' -> 'Friendly Center'. Generic heuristic (no
    per-mall hardcoding): split the domain's first label, then insert a
    space before a trailing mall-type word if one is glued onto it."""
    label = netloc.removeprefix("www.").split(".")[0]
    label = re.sub(
        r"(center|centre|mall|outlets?|square|commons|crossing|corner|market|place|plaza|towne?|village)$",
        r" \1", label, flags=re.IGNORECASE,
    )
    return " ".join(w.capitalize() for w in label.split())


def geometry_hash(geometry: dict | None) -> str | None:
    """Stable fingerprint of a polygon's shape, independent of which vertex it
    starts on or which way its ring winds — two GeoJSON encodings of the same
    physical footprint must hash identically, or every re-scrape of an
    unchanged store would look like a geometry change to geodiff.py."""
    if not geometry:
        return None
    try:
        g = _shapely_shape(geometry).normalize()
    except Exception:
        return None
    return hashlib.sha256(_shapely_wkb.dumps(g)).hexdigest()


def venue_info(venue: str) -> dict:
    """Human-facing metadata for a venue, incl. links back to the mall's own
    site so the dashboard can always point at its source of truth.

    Three venue shapes are supported:
      - a Simon/Mappedin slug, e.g. "simon-albertville"
      - a full directory-page URL for a Mapplic-powered mall,
        e.g. "https://www.friendlycenter.com/directory"
      - a full directory-page URL for Mall of America (dedicated one-off API)
    """
    if is_moa_venue(venue):
        parsed = urlparse(venue)
        return {
            "slug": venue,
            "name": "Mall of America",
            "site": f"{parsed.scheme}://{parsed.netloc}",
            "stores_url": venue,
            "map_url": venue,
        }

    if venue.startswith("http"):
        parsed = urlparse(venue)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return {
            "slug": venue,
            "name": _prettify_domain(parsed.netloc),
            "site": base,
            "stores_url": venue,
            "map_url": venue,
        }

    outlet = venue.split("-", 1)[1] if venue.startswith("simon-") else venue
    pretty = " ".join(w.capitalize() for w in outlet.replace("_", "-").split("-"))
    base = f"https://www.premiumoutlets.com/outlet/{outlet}"
    return {
        "slug": venue,
        "name": f"{pretty} Premium Outlets",
        "site": base,
        "stores_url": f"{base}/stores",
        "map_url": f"{base}/map/",
    }

MAPPEDIN_KEY = os.environ.get("MAPPEDIN_KEY", "NTdkMDQ3MjEwMDQyZDUwMmQyMDAwMDAw")
MAPPEDIN_SECRET = os.environ.get("MAPPEDIN_SECRET", "MjUwM2M3ZjM3N2Y5NzliNWZlYmE4YzU5ZjY2NWE4Y2M=")
MAPPEDIN_API = "https://api-gateway.mappedin.com"
STORE_UNIT_RE = re.compile(r"^[A-Z]\d{2,3}[A-Z]?$")


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
    m = re.search(r"/outlet/([a-z0-9\-]+)", url, re.IGNORECASE)
    return f"simon-{m.group(1).lower()}" if m else None


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


def parse_mappedin_venue(venue: str, on_progress=None) -> list[dict]:
    """Full store directory + unit codes for a Mappedin venue. See the CLI
    script for the annotated version; this is the same 2-call logic.

    on_progress(step_id, message, **extra) is called at each stage so callers
    (the dashboard's live console) can show what's actually happening instead
    of a spinner. Optional — the CLI passes nothing and behaves as before.
    """
    def p(step, msg, **extra):
        if on_progress:
            on_progress(step, msg, **extra)

    headers = _mi_headers()

    p("locations", "Asking Mappedin for the venue's store list…")
    loc_url = f"{MAPPEDIN_API}/public/1/location/{venue}?fields=id,externalId,name,type,states"
    locations = json.loads(_http_get(loc_url, headers).decode("utf-8"))
    status_by_name: dict[str, str] = {
        loc.get("name"): _status_from_states(loc.get("states"))
        for loc in locations
        if loc.get("type") == "store" and loc.get("name")
    }
    raw_by_name: dict[str, dict] = {
        loc.get("name"): loc
        for loc in locations
        if loc.get("type") == "store" and loc.get("name")
    }
    store_names = set(status_by_name)
    if not store_names:
        raise RuntimeError(f"Mappedin returned no stores for venue '{venue}'")
    coming = sum(1 for v in status_by_name.values() if v != "Open")
    p("locations", f"Got {len(store_names)} stores ({coming} not yet open). "
                   f"Filtered out amenities like restrooms and parking.",
      count=len(store_names))

    p("bundle", "Requesting the venue map bundle (holds the unit numbers)…")
    bundle_meta = json.loads(
        _http_get(f"{MAPPEDIN_API}/exports/mvf2/1/bundle?venue={venue}&version=1.0.0", headers).decode("utf-8")
    )
    zip_url = (
        (bundle_meta.get("perspectives", {}).get("Website", {}) or {}).get("url")
        or bundle_meta.get("url")
    )
    if not zip_url:
        raise RuntimeError("Mappedin bundle response contained no zip URL")

    p("bundle", "Downloading the map data zip…")
    zip_bytes = _http_get(zip_url, headers, timeout=120)
    p("bundle", f"Downloaded {len(zip_bytes)/1024:.0f} KB of map data.", kb=round(len(zip_bytes)/1024))

    p("spaces", "Reading the floor plan to find each store's unit…")
    space_features: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if name.startswith("space/") and name.endswith(".geojson"):
                data = json.loads(zf.read(name).decode("utf-8"))
                space_features.extend(data.get("features", []))
    p("spaces", f"Scanned {len(space_features)} mapped shapes.", features=len(space_features))

    unit_by_name: dict[str, str] = {}
    geometry_by_name: dict[str, dict] = {}
    for feat in space_features:
        props = feat.get("properties", {}) or {}
        name = (props.get("details", {}) or {}).get("name")
        ext = props.get("externalId")
        if name in store_names and ext and STORE_UNIT_RE.match(ext) and name not in unit_by_name:
            unit_by_name[name] = ext
            geometry = feat.get("geometry")
            if geometry:
                geometry_by_name[name] = geometry
    p("spaces", f"Matched {len(unit_by_name)} of {len(store_names)} stores to a unit number.",
      matched=len(unit_by_name))

    stores = []
    for name in sorted(store_names):
        unit = unit_by_name.get(name)
        geometry = geometry_by_name.get(name)
        stores.append({
            "name": name, "slug": None, "floor": unit,
            "location_in_outlet": unit, "category": None,
            "status": status_by_name.get(name, "Open"),
            "raw": raw_by_name.get(name),
            # Real-world footprint polygon (WGS84 lon/lat), straight from
            # Mappedin's own venue export — already in the zip we download for
            # unit numbers, previously discarded. See geodiff.py/geoexport.py.
            "geometry": geometry,
            "geometry_crs": "EPSG:4326" if geometry else None,
            "geometry_hash": geometry_hash(geometry),
            "source_geometry": {"provider": "mappedin", "external_id": unit} if geometry else None,
        })
    return stores


# --- Mapplic path (Friendly Center & other Mapplic-powered malls) -----------
#
# Mapplic embeds a `data-json="https://mapplic.com/getMapData?id=..."`
# attribute on the map container; that JSON in turn references a
# `settings.csv` asset URL holding the ENTIRE tenant directory as a flat
# CSV. No bot challenge, no per-store fetching.

def _svg_transform_fn(transform: str | None):
    """Parses the only transform shapes seen live on Mapplic SVGs:
    'translate(tx,ty) rotate(deg)' (rotate around origin, applied after
    translate — matches SVG's left-to-right transform composition). Returns
    an (x, y) -> (x, y) function; identity if transform is absent/unrecognized
    (callers are expected to warn on the unrecognized case, not this helper)."""
    if not transform:
        return lambda x, y: (x, y)
    tx = ty = 0.0
    deg = 0.0
    m = re.search(r"translate\(\s*([-\d.]+)[ ,]+([-\d.]+)\s*\)", transform)
    if m:
        tx, ty = float(m.group(1)), float(m.group(2))
    m = re.search(r"rotate\(\s*([-\d.]+)", transform)
    if m:
        deg = float(m.group(1))
    rad = math.radians(deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def fn(x, y):
        x, y = x + tx, y + ty
        return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)

    return fn


def _svg_path_points(d: str) -> list[tuple[float, float]]:
    """Straight-line-segment approximation of an SVG path's M/L/H/V/C/Z
    commands — enough fidelity for simple mall-unit outlines (rectangles and
    near-rectangles), not general bezier-accurate rendering. Curve commands
    (C/S/Q/T) are approximated by their endpoint only, dropping control
    points, which is fine for footprint-area/overlap comparisons."""
    tokens = re.findall(r"([MLHVCSQTZmlhvcsqtz])|(-?\d*\.?\d+(?:e-?\d+)?)", d)
    pts: list[tuple[float, float]] = []
    cmd = None
    cur = [0.0, 0.0]
    nums: list[float] = []

    def args_needed(c):
        return {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "Z": 0}.get(c.upper(), 2)

    for letter, num in tokens:
        if letter:
            cmd = letter
            nums = []
            if cmd.upper() == "Z":
                if pts:
                    pts.append(pts[0])
            continue
        nums.append(float(num))
        if len(nums) == args_needed(cmd):
            relative = cmd.islower()
            C = cmd.upper()
            if C == "H":
                x = cur[0] + nums[0] if relative else nums[0]
                y = cur[1]
            elif C == "V":
                x = cur[0]
                y = cur[1] + nums[0] if relative else nums[0]
            elif C in ("M", "L", "T"):
                x, y = nums
                if relative:
                    x, y = cur[0] + x, cur[1] + y
            elif C in ("C", "S", "Q"):
                x, y = nums[-2], nums[-1]
                if relative:
                    x, y = cur[0] + x, cur[1] + y
            else:
                nums = []
                continue
            cur = [x, y]
            pts.append((x, y))
            nums = []
    return pts


def _svg_shape_to_polygon(shape: dict) -> dict | None:
    """A parsed SVG <rect>/<path> (see _fetch_mapplic_shapes) -> a GeoJSON
    Polygon in the SVG's own local pixel space."""
    fn = _svg_transform_fn(shape.get("transform"))
    if shape["kind"] == "rect":
        x, y = shape["x"], shape["y"]
        w, h = shape["width"], shape["height"]
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    else:  # path
        corners = _svg_path_points(shape["d"])
        if len(corners) < 3:
            return None
        if corners[0] != corners[-1]:
            corners = corners + [corners[0]]
    ring = [list(fn(x, y)) for x, y in corners]
    return {"type": "Polygon", "coordinates": [ring]}


def _fetch_mapplic_shapes(map_data: dict, on_progress=None) -> tuple[dict[str, dict], dict]:
    """Downloads every SVG referenced in map_data['layers'] and returns
    {location_id: {"kind": "rect"|"path", ...raw attrs, "transform": ...}},
    plus the pixel dimensions of the (first) map SVG for context."""
    def p(step, msg, **extra):
        if on_progress:
            on_progress(step, msg, **extra)

    shapes: dict[str, dict] = {}
    map_size: dict = {}
    for layer in (map_data.get("layers") or []):
        svg_url = layer.get("file")
        if not svg_url or not svg_url.lower().split("?")[0].endswith(".svg"):
            continue
        try:
            svg_bytes = _http_get(svg_url, headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            p("shapes", f"Couldn't fetch layer SVG '{layer.get('id')}': {e}", level="warn")
            continue
        try:
            root = ET.fromstring(svg_bytes)
        except ET.ParseError as e:
            p("shapes", f"Layer SVG '{layer.get('id')}' didn't parse as XML: {e}", level="warn")
            continue

        if not map_size:
            vb = root.get("viewBox")
            if vb:
                parts = vb.split()
                if len(parts) == 4:
                    map_size = {"width": float(parts[2]), "height": float(parts[3])}
            elif root.get("width") and root.get("height"):
                try:
                    map_size = {"width": float(root.get("width")), "height": float(root.get("height"))}
                except ValueError:
                    pass

        for el in root.iter():
            tag = el.tag.split("}")[-1]
            loc_id = el.get("id")
            if not loc_id or loc_id in shapes:
                continue
            transform = el.get("transform")
            if tag == "rect":
                try:
                    shapes[loc_id] = {
                        "kind": "rect", "transform": transform,
                        "x": float(el.get("x", 0)), "y": float(el.get("y", 0)),
                        "width": float(el.get("width", 0)), "height": float(el.get("height", 0)),
                    }
                except ValueError:
                    continue
            elif tag == "path" and el.get("d"):
                shapes[loc_id] = {"kind": "path", "transform": transform, "d": el.get("d")}
    return shapes, map_size


def mapplic_getmapdata_url_from_page(url: str) -> str | None:
    """Fetch a page's raw HTML and look for its Mapplic map embed URL.
    Returns None if the page isn't Mapplic-powered."""
    try:
        html = _http_get(url, headers={"User-Agent": "Mozilla/5.0"}).decode("utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r'(https://mapplic\.com/getMapData\?id=[A-Za-z0-9_-]+)', html)
    return m.group(1) if m else None


def parse_mapplic_venue(url: str, on_progress=None) -> list[dict]:
    """Full store directory for a Mapplic-powered mall's directory page URL.

    Columns seen (Friendly Center): "Property Name","id","title","link",
    "about","desc","image","layer","style","disable","hide","group","sample"
    - disable=="TRUE" or title=="VACANT" -> not a real tenant, skip.
    - group is a comma-separated tag list; doubles as category and carries
      status tags like "Coming Soon" / "Now Open" / "Temporarily Closed".
    - id is the map's own unit code (e.g. "u103").
    """
    def p(step, msg, **extra):
        if on_progress:
            on_progress(step, msg, **extra)

    p("locations", "Looking for the mall's Mapplic map embed…")
    getmapdata_url = mapplic_getmapdata_url_from_page(url)
    if not getmapdata_url:
        raise RuntimeError(f"No Mapplic map embed found on {url}")

    p("locations", "Fetching the map's config…")
    map_data = json.loads(_http_get(getmapdata_url, headers={"User-Agent": "Mozilla/5.0"}))
    csv_url = (map_data.get("settings") or {}).get("csv")
    if not csv_url:
        raise RuntimeError("Mapplic map data has no settings.csv URL")

    p("locations", "Downloading the tenant directory CSV…")
    csv_bytes = _http_get(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    rows = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))

    p("shapes", "Reading the map's SVG for each unit's exact footprint…")
    svg_shapes, map_size = _fetch_mapplic_shapes(map_data, on_progress=on_progress)
    p("shapes", f"Found {len(svg_shapes)} mapped shapes on the floor plan.", shapes=len(svg_shapes))

    status_tags = ("Coming Soon", "Now Open", "Temporarily Closed")
    stores = []
    matched_shapes = 0
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title or title.upper() == "VACANT":
            continue
        if (row.get("disable") or "").strip().upper() == "TRUE":
            continue

        tags = [t.strip() for t in (row.get("group") or "").split(",") if t.strip()]
        status = next((t for t in tags if t in status_tags), "Open")
        category = ", ".join(t for t in tags if t not in status_tags) or None

        loc_id = (row.get("id") or "").strip() or None
        svg_shape = svg_shapes.get(loc_id) if loc_id else None
        geometry = _svg_shape_to_polygon(svg_shape) if svg_shape else None
        if geometry:
            matched_shapes += 1

        stores.append({
            "name": title,
            "slug": (row.get("link") or "").strip() or None,
            "floor": None,
            "location_in_outlet": loc_id,
            "category": category,
            "status": status,
            "raw": dict(row),
            # Pixel-space footprint from the mall's own SVG floor plan — no
            # lon/lat anchor exists in Mapplic's data, so this can't be
            # auto-georeferenced (see geodiff.py's CRS guard). source_geometry
            # keeps the un-converted SVG shape so a future fix to
            # _svg_shape_to_polygon can re-derive from ground truth.
            "geometry": geometry,
            "geometry_crs": "SVG_PIXEL" if geometry else None,
            "geometry_hash": geometry_hash(geometry),
            "source_geometry": {"provider": "mapplic", "type": f"svg_{svg_shape['kind']}", "raw": svg_shape}
                if svg_shape else None,
            "map_size": map_size or None,
        })
    stores.sort(key=lambda x: x["name"])
    p("locations", f"Got {len(stores)} stores from the CSV directory "
                   f"({matched_shapes} matched to an exact footprint).", count=len(stores))
    return stores


def is_mapplic_venue(venue: str) -> bool:
    """Venue-shape check: a full URL (that isn't Mall of America's dedicated
    path) means Mapplic; a bare slug means Simon/Mappedin. Used by the webapp
    to route to the right parser."""
    return venue.startswith("http") and not is_moa_venue(venue)


# --- Mall of America (dedicated one-off API, not a reusable mall template) --
#
# moaapi.net is a public REST API specific to this mall — hardcoded here
# rather than sniffed from the page, unlike Mappedin/Mapplic which power many
# malls. One unauthenticated GET returns the entire tenant list, incl. unit
# codes and floor level.

MOA_TENANTS_URL = "https://www.moaapi.net/tenants.php"
_MOA_CLOSED_STATUSES = {"Closed", "Closed for Remodeling"}


def is_moa_venue(venue: str) -> bool:
    return venue.startswith("http") and "mallofamerica.com" in urlparse(venue).netloc.lower()


def parse_moa_venue(venue: str | None = None, on_progress=None) -> list[dict]:
    """
    moaapi.net/tenants.php returns every tenant AND non-tenant landmark
    (elevators, parkway signage, etc. — these have no status/categories and
    are filtered out). Each real tenant carries location.unit_number (the
    unit code) and level (floor) directly.

    `venue` is accepted-but-unused so this has the same call signature as
    parse_mappedin_venue/parse_mapplic_venue for the webapp's dispatch code.
    """
    def p(step, msg, **extra):
        if on_progress:
            on_progress(step, msg, **extra)

    p("locations", "Downloading Mall of America's public tenant directory…")
    data = json.loads(_http_get(MOA_TENANTS_URL, headers={"User-Agent": "Mozilla/5.0"}))

    stores = []
    for t in data:
        status_name = (t.get("status") or {}).get("name")
        if not status_name or not t.get("categories"):
            continue  # landmark entry, not a real tenant
        if status_name in _MOA_CLOSED_STATUSES:
            continue

        loc = t.get("location") or {}
        stores.append({
            "name": t.get("name"),
            "slug": t.get("url_slug"),
            "floor": str(t["level"]) if t.get("level") is not None else None,
            "location_in_outlet": loc.get("unit_number"),
            "category": ", ".join(c["name"] for c in t.get("categories", []) if c.get("name")) or None,
            "status": "Open" if status_name == "Normal" else status_name,
            # Kept raw (minus the heavy media/hours blobs) so the dashboard can
            # facet-filter on fields we don't normalize, e.g. `type` — this is
            # what distinguishes real Retail/Dining/Attraction tenants from
            # rides, parking, offices, and facilities that the API also lists.
            "raw": {k: v for k, v in t.items() if k not in ("media", "hours")},
            # No geometry source found in tenants.php (only unit_number/level).
            # MoA's interactive map almost certainly has its own geometry
            # API/SVG, not yet reverse-engineered — explicit None rather than
            # omitted so every parser hands geodiff.py the same shape.
            "geometry": None,
            "geometry_crs": None,
            "geometry_hash": None,
            "source_geometry": None,
        })

    stores.sort(key=lambda x: x["name"])
    p("locations", f"Got {len(stores)} stores from the tenant directory.", count=len(stores))
    return stores


def venue_id_component(venue: str) -> str:
    """Filesystem/id-safe form of a venue for building run IDs. Simon slugs
    already are; a full URL (Mapplic venues) contains '/' and ':' which would
    otherwise break the local-file storage backend's filenames."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", venue).strip("_")


# --- diff -------------------------------------------------------------------

def _store_key(s: dict) -> str:
    return s.get("slug") or s.get("name") or ""


def _unit_of(s: dict):
    return s.get("location_in_outlet") or s.get("floor")


def diff_stores(old: list[dict] | None, new: list[dict]) -> dict:
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
