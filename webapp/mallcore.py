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
import zipfile
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

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
    for feat in space_features:
        props = feat.get("properties", {}) or {}
        name = (props.get("details", {}) or {}).get("name")
        ext = props.get("externalId")
        if name in store_names and ext and STORE_UNIT_RE.match(ext) and name not in unit_by_name:
            unit_by_name[name] = ext
    p("spaces", f"Matched {len(unit_by_name)} of {len(store_names)} stores to a unit number.",
      matched=len(unit_by_name))

    stores = []
    for name in sorted(store_names):
        unit = unit_by_name.get(name)
        stores.append({
            "name": name, "slug": None, "floor": unit,
            "location_in_outlet": unit, "category": None,
            "status": status_by_name.get(name, "Open"),
        })
    return stores


# --- Mapplic path (Friendly Center & other Mapplic-powered malls) -----------
#
# Mapplic embeds a `data-json="https://mapplic.com/getMapData?id=..."`
# attribute on the map container; that JSON in turn references a
# `settings.csv` asset URL holding the ENTIRE tenant directory as a flat
# CSV. No bot challenge, no per-store fetching.

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
    stores.sort(key=lambda x: x["name"])
    p("locations", f"Got {len(stores)} stores from the CSV directory.", count=len(stores))
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
