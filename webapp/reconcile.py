"""
reconcile — checks the primary Mappedin snapshot for completeness against
Simon's own /stores directory page.

Why this exists: Mappedin's venue map can lag Simon's own site for newly
added tenants. Confirmed case: "Ann Taylor Factory Store" existed on
premiumoutlets.com (with a "Coming Soon" badge) but was completely absent
from the Mappedin API — not a parsing bug, a genuine gap between the two
upstream systems. Mappedin stays the fast/free PRIMARY source for routine
runs; this module is an optional, on-demand SECOND source used purely to
catch and surface that kind of drift.

Deliberately scrapes ONLY the /stores directory page (name + status) via
Hyperbrowser's stealth session — never per-store detail pages. That's the
same page a human filtering by "Coming Soon" would see, fetched once, so it
stays fast and doesn't trigger the bot wall the old bulk per-store approach
hit. It does NOT return unit codes; Mappedin remains the source for those.

Requires HYPERBROWSER_API_KEY. If unset (or the package isn't installed),
available() returns False and callers should hide/disable this feature
rather than fail the primary Mappedin flow.
"""

import os
import re

HYPERBROWSER_API_KEY = os.environ.get("HYPERBROWSER_API_KEY")


def available() -> bool:
    if not HYPERBROWSER_API_KEY:
        return False
    try:
        import hyperbrowser  # noqa: F401
        return True
    except ImportError:
        return False


def simon_url_for_venue(venue: str) -> str:
    """simon-albertville -> https://www.premiumoutlets.com/outlet/albertville/stores"""
    outlet = venue.split("-", 1)[1] if venue.startswith("simon-") else venue
    return f"https://www.premiumoutlets.com/outlet/{outlet}/stores"


def _parse_simon_stores_page(content: str) -> list[dict]:
    """Name + status only. Mirrors get_store_list_unified.py's
    parse_simon_stores_page — duplicated (not imported) so webapp/ stays a
    self-contained deployable unit, same pattern as parse_mappedin_venue."""
    blocks = re.split(r"(?=\[!\[)", content)
    slug_re = re.compile(r"stores/([a-z0-9\-]+)[\s\"/]")
    name_re = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
    floor_re = re.compile(r"\[([^\]]+?)\]\(https://[^)]*?/map/#/profile\?location=")
    center_map_re = re.compile(r"\[VIEW ON CENTER MAP\]\(https://[^)]*?/map\b")

    stores: dict[str, dict] = {}
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
        name = re.sub(r"\\+|\s+", " ", name_m.group(1)).strip()
        name = re.sub(r"\s*Coming Soon\s*$", "", name, flags=re.IGNORECASE).strip()

        floor_m = floor_re.search(block)
        center_map_m = center_map_re.search(block)
        if floor_m is None and center_map_m is None:
            continue

        link_end = (floor_m or center_map_m).end()
        is_coming_soon = bool(re.search(r"coming soon", block[:link_end], re.IGNORECASE))
        stores[slug] = {"name": name, "slug": slug, "status": "Coming Soon" if is_coming_soon else "Open"}

    return sorted(stores.values(), key=lambda x: x["name"])


def fetch_simon_store_names(venue: str) -> list[dict]:
    """One page load via Hyperbrowser stealth -> [{name, slug, status}]. Raises
    on failure (network error, bot wall, or a 0-result parse that likely means
    the page structure changed and the regex needs updating)."""
    from hyperbrowser import Hyperbrowser
    from hyperbrowser.models import StartScrapeJobParams, ScrapeOptions

    url = simon_url_for_venue(venue)
    client = Hyperbrowser(api_key=HYPERBROWSER_API_KEY)
    result = client.scrape.start_and_wait(
        StartScrapeJobParams(
            url=url,
            scrape_options=ScrapeOptions(formats=["markdown"]),
            session_options={"use_stealth": True},
        )
    )
    if result.status != "completed":
        raise RuntimeError(f"Simon page scrape failed: status={result.status}")

    stores = _parse_simon_stores_page(result.data.markdown or "")
    if not stores:
        raise RuntimeError("Simon page scrape returned 0 stores — page structure may have changed")
    return stores


def reconcile(mappedin_stores: list[dict], venue: str) -> dict:
    """Compare a Mappedin store list against a fresh Simon-site scrape.
    Returns counts and, by name, which stores exist on the official site but
    are missing from Mappedin (the drift we're actually trying to catch)."""
    simon_stores = fetch_simon_store_names(venue)
    mappedin_names = {s["name"] for s in mappedin_stores}
    simon_names = {s["name"] for s in simon_stores}

    missing_from_mappedin = sorted(simon_names - mappedin_names)
    extra_in_mappedin = sorted(mappedin_names - simon_names)

    return {
        "venue": venue,
        "simon_url": simon_url_for_venue(venue),
        "mappedin_count": len(mappedin_stores),
        "simon_count": len(simon_stores),
        "in_sync": not missing_from_mappedin and not extra_in_mappedin,
        "missing_from_mappedin": missing_from_mappedin,
        "extra_in_mappedin": extra_in_mappedin,
    }
