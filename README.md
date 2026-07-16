# Mall Directory Monitor

Pull the **complete store directory for shopping malls — every store plus its exact unit code** (e.g. `NIKE Factory Store → A010`) — and **track how it changes over time**, via a CLI scraper and a hostable web dashboard.

---

## Why

Malls list their stores online, but the public directory page usually **omits unit/location codes**. Those live on each store's individual page — which, on Simon Premium Outlets, sits behind a **"Press & Hold" bot challenge**. Scraping 60+ store pages per mall is therefore slow, fragile, and frequently blocked.

**The shortcut:** the mall's interactive map is powered by **[Mappedin](https://www.mappedin.com/)**, whose API is unauthenticated. **Two API calls** return the entire dataset — all stores *and* their unit codes — with no browser, no per-store scraping, and no bot challenge. That's the primary path this project uses.

---

## What's in here

```
.
├── get_store_list_unified.py     # CLI scraper (tiered: Mappedin → template → AI)
├── render.yaml                   # Render Blueprint for auto-deploy
├── README.md
└── webapp/                       # hostable dashboard
    ├── server.py                 # Flask API + serves the page
    ├── mallcore.py               # stdlib-only Mappedin fetch + diff
    ├── storage.py                # run history: Postgres (prod) or JSON files (dev)
    ├── static/index.html         # the dashboard UI
    ├── venues.json               # which venues to track
    ├── requirements.txt
    └── Procfile
```

---

## How it works

### The data source (Mappedin)

Two unauthenticated calls per venue:

1. `GET /public/1/location/{venue}` → the authoritative list of stores.
2. `GET /exports/mvf2/1/bundle?venue={venue}` → a signed zip whose `space/*.geojson`
   carries each store's **unit code** (`externalId`) joined to its name.

A store's unit code looks like `A010`, `B260A`, `C100`; amenities (restrooms, ATMs, parking) use non-store codes and are filtered out.

### The CLI: tiered strategy

`get_store_list_unified.py` tries, in order:

1. **Mappedin API** — fast, free, includes unit codes, no bot challenge. *(primary)*
2. **Browser scraper + template parser** — for Simon pages not on Mappedin. *(fallback)*
3. **AI extraction** — semantic extraction for any unknown site. *(last resort)*

It writes a `.stores.json` + a self-contained `.stores.html` report, and **diffs against the previous run** (stores added / removed / relocated).

### The dashboard

`webapp/` is a Flask app: a **Run now** button (live scrape), a **Previous runs** browser, per-run **change detection**, a searchable table, and light/dark themes.

---

## Quick start

### CLI

```bash
# Fast path — no API key needed (Mappedin):
python get_store_list_unified.py "https://www.premiumoutlets.com/outlet/albertville/stores"

# Non-Simon Mappedin mall — pass the venue slug explicitly:
python get_store_list_unified.py "<url>" --mappedin-venue "simon-<location>"

# Force the browser scraper fallback (needs HYPERBROWSER_API_KEY):
python get_store_list_unified.py "<url>" --no-mappedin
```

The scraper/AI fallback needs a [Hyperbrowser](https://hyperbrowser.ai/) key:
`export HYPERBROWSER_API_KEY=...` (only if the Mappedin path doesn't apply).

### Web dashboard (local)

```bash
cd webapp
pip install -r requirements.txt
python server.py            # -> http://127.0.0.1:5000
```

With no database configured it stores run history as JSON files in `webapp/runs/`.
`GET /api/health` reports the active backend.

---

## Deploy (GitHub → Render, history in Supabase)

Run history **must** live in an external DB — Render's filesystem is wiped on every
deploy, so flat files would lose all history. Storage auto-switches on `DATABASE_URL`:
set → Postgres, unset → local files.

1. **Push to GitHub**

   ```bash
   git init && git add . && git commit -m "Mall directory dashboard"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **Create a Supabase DB**, copy its **Transaction pooler** URI
   (Project → Connect → ORMs/URI → *Transaction pooler*, port `6543`):

   ```
   postgresql://postgres.<ref>:<PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```

3. **Deploy on Render:** New + → **Blueprint** → pick the repo. `render.yaml` creates
   the web service and auto-deploys on every push. In the service's **Environment**, set:

   | Var | Value |
   |---|---|
   | `DATABASE_URL` | the Supabase pooler URI above |
   | `MAPPEDIN_KEY` / `MAPPEDIN_SECRET` | *(optional — defaults are built in)* |

On first boot the `runs` table (with a `jsonb` snapshot column) is created automatically.
The DB starts empty — press **Run now**, or hit `POST /api/run` on a schedule to populate it.

---

## Storage / "the log"

Every run is stored as one immutable record — **that history is the log; nothing is
overwritten.**

- **Production:** Postgres, table `runs(id, venue, generated, doc jsonb, created_at)`.
- **Dev:** one JSON file per run in `webapp/runs/`.

Each record holds that run's full store list **and** its diff versus the prior run,
so "Previous runs" can walk the whole timeline.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the dashboard |
| `GET` | `/api/venues` | configured venue slugs |
| `GET` | `/api/runs?venue=` | run history (newest first) |
| `GET` | `/api/run/<id>` | one full snapshot |
| `POST` | `/api/run` `{venue}` | scrape now, diff, save, return |
| `GET` | `/api/health` | `{ ok, storage }` |

---

## Adding malls

- **Another Simon outlet:** just use its `/stores` URL — the venue slug (`simon-<outlet>`)
  is derived automatically.
- **Another Mappedin operator:** open one of their venues' `/map` page → DevTools →
  Network → filter `mappedin` → copy the `x-mappedin-key` / `x-mappedin-secret` headers
  and the venue slug. Set the keys via env vars and pass `--mappedin-venue`.
- **A non-Mappedin mall:** the CLI falls back to the scraper/AI path.

For the dashboard, add venue slugs to `webapp/venues.json`.

---

## Notes & caveats

- **Unit-code pattern.** `STORE_UNIT_RE = ^[A-Z]\d{2,3}[A-Z]?$` fit Simon cleanly. Other
  operators may use a different scheme — spot-check and widen it if legit stores drop out.
- **Keys can rotate.** If Mappedin calls start returning 401, grab fresh
  `MAPPEDIN_KEY`/`MAPPEDIN_SECRET` from any `/map` page's network requests.
- **Undocumented API.** These endpoints are public and unauthenticated, but private and
  unpromised — they can change without notice, which is exactly why the tiered fallback
  exists. For commercial/high-volume use, license Mappedin properly and be a polite client
  (cache, pace requests, refresh monthly rather than constantly).
- **This is not a security bypass** — no challenge is defeated; it reads a public data feed
  the mall's own map already uses.
