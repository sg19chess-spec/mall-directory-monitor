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
import time
import random

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


class BotWallError(RuntimeError):
    """Simon's PerimeterX "Press & Hold" challenge served instead of the page.
    Distinct from a parse failure: nothing is wrong with our code or their
    markup, the request simply didn't get through this time."""


def _looks_like_bot_wall(markdown: str) -> bool:
    low = markdown.lower()
    return (
        "press & hold" in low
        or "confirm you are" in low
        or "access to this page has been denied" in low
    )


def fetch_simon_store_names(venue: str, attempts: int = 3) -> list[dict]:
    """One page load via Hyperbrowser stealth -> [{name, slug, status}].

    Stealth is PROBABILISTIC, not reliable — observed live: the identical
    request succeeded (55KB, 61 stores) and then minutes later got served the
    Press & Hold challenge instead. So retry a few times with backoff before
    giving up, and distinguish the two failure modes, because they need
    completely different responses:
      * BotWallError  -> transient; just try again later. Nothing to fix.
      * RuntimeError  -> we got a real page but parsed 0 stores, which does
                         suggest their markup changed and the regex needs work.
    """
    from hyperbrowser import Hyperbrowser
    from hyperbrowser.models import StartScrapeJobParams, ScrapeOptions

    url = simon_url_for_venue(venue)
    client = Hyperbrowser(api_key=HYPERBROWSER_API_KEY)

    for attempt in range(1, attempts + 1):
        result = client.scrape.start_and_wait(
            StartScrapeJobParams(
                url=url,
                scrape_options=ScrapeOptions(formats=["markdown"]),
                session_options={"use_stealth": True},
            )
        )
        if result.status != "completed":
            if attempt == attempts:
                raise RuntimeError(f"Simon page scrape failed: status={result.status}")
            time.sleep(random.uniform(3, 7))
            continue

        markdown = (result.data.markdown or "") if result.data else ""

        if _looks_like_bot_wall(markdown):
            if attempt == attempts:
                raise BotWallError(
                    f"Simon's bot challenge blocked the check after {attempts} attempts. "
                    "This is transient — the Mappedin snapshot itself is unaffected; "
                    "just try the completeness check again in a bit."
                )
            time.sleep(random.uniform(4, 9))
            continue

        stores = _parse_simon_stores_page(markdown)
        if not stores:
            # A real page (no bot wall) that yields nothing => our regex is stale.
            raise RuntimeError(
                f"Fetched Simon's page ({len(markdown)} chars, no bot challenge) but "
                "parsed 0 stores — their page structure likely changed and "
                "_parse_simon_stores_page needs updating."
            )
        return stores

    raise BotWallError("Simon page scrape exhausted all attempts.")


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
