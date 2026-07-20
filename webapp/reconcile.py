"""
reconcile — checks a mall's primary API/CSV snapshot for completeness against
a live Hyperbrowser scrape of that mall's own public directory page.

Why this exists: every primary source here (Mappedin, Mapplic's CSV, MoA's
tenants API) is a separate system from the page a shopper actually looks at,
and they drift from and disagree with each other in both directions:
  - Confirmed case (Simon): "Ann Taylor Factory Store" was on premiumoutlets.com
    with a "Coming Soon" badge but completely absent from the Mappedin API —
    a gap the primary source doesn't know about.
  - Confirmed case (Mall of America): moaapi.net returns 552 tenants but the
    live /directory page only lists ~512 — the API also carries rides, parking
    ramps, back-offices, and facilities that aren't "stores" on the page.
This module surfaces BOTH directions — missing_from_api (on the live page,
not in our pull) and extra_in_api (in our pull, not on the live page) — as a
report, not a silent filter. Which of those are "real" differences worth
excluding is a judgment call left to whoever's looking at the dashboard.

Simon pages get a fast, hand-verified markdown-regex parser (name + status
only). Any other venue (a full directory URL — Mapplic, MoA, or anything
else added later) falls back to Hyperbrowser's AI extraction job, since we
don't have a hand-tuned parser for every possible page layout.

Requires HYPERBROWSER_API_KEY. If unset (or the package isn't installed),
available() returns False and callers should hide/disable this feature
rather than fail the primary snapshot flow.
"""

import os
import re
import time
import random

HYPERBROWSER_API_KEY = os.environ.get("HYPERBROWSER_API_KEY")

EXTRACT_PROMPT = (
    "This is a shopping mall's store directory page. Extract every store, "
    "restaurant, or tenant listed. For each one, return its name and status "
    "if shown (e.g. Open, Closed, Coming Soon, Temporarily Closed). Do not "
    "include navigation links, ads, rides/attractions info panels, or "
    "unrelated page elements — only actual tenant/store listings."
)
STORE_NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "stores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": ["string", "null"]},
                },
                "required": ["name"],
            },
        }
    },
    "required": ["stores"],
}


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


def directory_url_for_venue(venue: str) -> str:
    """The live, human-facing directory page to cross-check against. A full
    URL venue (Mapplic/MoA/anything future) already IS that page; a Simon
    slug needs its /stores URL built."""
    return venue if venue.startswith("http") else simon_url_for_venue(venue)


def _norm(name: str) -> str:
    """Loose match key: lowercase, strip punctuation/trademark symbols,
    collapse whitespace. Good enough to survive "Ann Taylor" vs "Ann Taylor®"
    or extra spacing without pulling in a fuzzy-matching dependency."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


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
    """The site's bot challenge (e.g. PerimeterX "Press & Hold") was served
    instead of the page. Distinct from a parse failure: nothing is wrong
    with our code or their markup, the request simply didn't get through."""


def _looks_like_bot_wall(markdown: str) -> bool:
    low = markdown.lower()
    return (
        "press & hold" in low
        or "confirm you are" in low
        or "access to this page has been denied" in low
    )


def _fetch_simon_store_names(client, venue: str, attempts: int, p) -> list[dict]:
    """Fast, hand-verified path for Simon's /stores page (markdown + regex,
    no AI extraction needed). Stealth is PROBABILISTIC — observed live: the
    identical request succeeded and then minutes later hit the Press & Hold
    challenge — so retry a few times with backoff before giving up."""
    from hyperbrowser.models import StartScrapeJobParams, ScrapeOptions

    url = simon_url_for_venue(venue)
    for attempt in range(1, attempts + 1):
        p("site", f"Opening the mall's own website in a real browser"
                  f"{f' (try {attempt} of {attempts})' if attempt > 1 else ''}…",
          attempt=attempt)
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
            p("site", f"Page didn't load (status: {result.status}). Retrying…", level="warn")
            time.sleep(random.uniform(3, 7))
            continue

        markdown = (result.data.markdown or "") if result.data else ""

        if _looks_like_bot_wall(markdown):
            if attempt == attempts:
                raise BotWallError(
                    f"Simon's bot challenge blocked the check after {attempts} attempts. "
                    "This is transient — the primary snapshot itself is unaffected; "
                    "just try the completeness check again in a bit."
                )
            p("site", "Hit the mall site's “Press & Hold” bot check. "
                      "Backing off and retrying…", level="warn")
            time.sleep(random.uniform(4, 9))
            continue

        p("site", f"Got through — read {len(markdown)/1024:.0f} KB of the live page.",
          kb=round(len(markdown)/1024))
        stores = _parse_simon_stores_page(markdown)
        if not stores:
            raise RuntimeError(
                f"Fetched Simon's page ({len(markdown)} chars, no bot challenge) but "
                "parsed 0 stores — their page structure likely changed and "
                "_parse_simon_stores_page needs updating."
            )
        coming = sum(1 for s in stores if s["status"] != "Open")
        p("site", f"The website lists {len(stores)} stores ({coming} coming soon).",
          count=len(stores))
        return stores

    raise BotWallError("Simon page scrape exhausted all attempts.")


def _fetch_generic_store_names(client, url: str, p) -> list[dict]:
    """Any non-Simon directory page: no hand-tuned parser exists, so read it
    with Hyperbrowser's AI extraction job instead (semantic, not regex —
    slower/costs more per call, but works on an arbitrary page layout)."""
    from hyperbrowser.models import StartExtractJobParams

    p("site", "Reading the mall's public directory page (AI extraction pass)…")
    result = client.extract.start_and_wait(
        StartExtractJobParams(
            urls=[url],
            prompt=EXTRACT_PROMPT,
            schema=STORE_NAME_SCHEMA,
            session_options={"use_stealth": True},
        )
    )
    if result.status != "completed":
        raise RuntimeError(f"Directory page extraction failed: status={result.status}, "
                            f"error={getattr(result, 'error', None)}")

    data = result.data
    raw_stores = data.get("stores", []) if isinstance(data, dict) else []
    stores = [
        {"name": s.get("name"), "status": (s.get("status") or "Open")}
        for s in raw_stores if s.get("name")
    ]
    if not stores:
        raise RuntimeError(
            f"Fetched {url} but the AI extractor found 0 stores — the page "
            "structure may need a dedicated parser instead."
        )
    coming = sum(1 for s in stores if s["status"] != "Open")
    p("site", f"The website lists {len(stores)} stores ({coming} not plain \"Open\").",
      count=len(stores))
    return stores


def fetch_site_store_names(venue: str, attempts: int = 3, on_progress=None) -> list[dict]:
    """One live page load -> [{name, status}], dispatched by venue shape."""
    from hyperbrowser import Hyperbrowser

    def p(step, msg, **extra):
        if on_progress:
            on_progress(step, msg, **extra)

    client = Hyperbrowser(api_key=HYPERBROWSER_API_KEY)
    if venue.startswith("http"):
        return _fetch_generic_store_names(client, venue, p)
    return _fetch_simon_store_names(client, venue, attempts, p)


def reconcile(api_stores: list[dict], venue: str, on_progress=None) -> dict:
    """Compare the primary-source store list against a fresh live scrape of
    the mall's own directory page. Returns counts and, by name, differences
    in BOTH directions — what's missing from the API pull, and what the API
    pull has that the live page doesn't (over-broad data, e.g. non-store
    entries like rides/parking/offices bundled into an API meant for more
    than just the shopper directory)."""
    site_stores = fetch_site_store_names(venue, on_progress=on_progress)

    api_by_key: dict[str, str] = {_norm(s["name"]): s["name"] for s in api_stores if s.get("name")}
    site_by_key: dict[str, str] = {_norm(s["name"]): s["name"] for s in site_stores if s.get("name")}

    missing_from_api = sorted(name for key, name in site_by_key.items() if key not in api_by_key)
    extra_in_api = sorted(name for key, name in api_by_key.items() if key not in site_by_key)

    return {
        "venue": venue,
        "site_url": directory_url_for_venue(venue),
        "api_count": len(api_stores),
        "site_count": len(site_stores),
        "in_sync": not missing_from_api and not extra_in_api,
        "missing_from_api": missing_from_api,
        "extra_in_api": extra_in_api,
    }
