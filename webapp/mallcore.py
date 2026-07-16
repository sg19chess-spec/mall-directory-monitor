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
import json
import zipfile
import urllib.request
from datetime import datetime, timezone

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


def venue_info(venue: str) -> dict:
    """Human-facing metadata for a venue slug, incl. links back to the mall's
    own site so the dashboard can always point at its source of truth."""
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
