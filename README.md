# Blind Trust — data pipeline

Self-maintaining backend for the Blind Trust app. GitHub Actions runs the
scrapers on a schedule, commits fresh JSON to `data/`, and emails you when new
CIEC filings appear. Zero servers, zero cost on a public repo.

## What runs when

| Task | Schedule | What it does |
|---|---|---|
| `registry` | every 6 h | Scrapes the CIEC public registry newest-first, stops at the first already-seen page, extracts securities + material-change diffs, emails alerts |
| `altdata` | nightly | Newsroom announcements + contract awards + lobbying spikes naming disclosed-held companies → Signals; Government Favour Index. Scanned back to `since_date` |
| `prices` | nightly | % change since disclosure date per matched ticker (yfinance) |
| `photos` | nightly | MP & Senator headshots → `photos.json` and chamber/party/riding → `roster.json` (see [Headshots & roster](#headshots--roster)) |
| `backfill` | manual | One-time deep sweep of the whole `since_date` window (below) |

Outputs in `data/`: `registry.json`, `tape.json`, `holdings.json`,
`signals.json`, `alpha.json`, `prices.json`, `photos.json`, `roster.json`,
`state.json`.

Signals coverage is source-limited: government releases naming a specific
public company are inherently sparse even scanned back to the start date, so
the tab is filled out with the contract-award and lobbying matches above.

### Coverage window (45th Parliament)

The registry crawl is bounded by `since_date` in `config.yaml` — default
`2025-04-28`, the start of the **45th Parliament** (general election; first
sitting / throne speech 2025-05-27). On the first run (or whenever `since_date`
changes) the registry task does a one-time **deep backfill**: it pages back
until it passes the cutoff, then keeps only disclosures on or after it. Later
runs are incremental (stop at the first already-seen page) but still enforce
the window. To force a fresh full sweep, dispatch the workflow with task
`backfill`. Widen history by moving `since_date` earlier; `max_records` caps the
rolling store.

## Setup (10 minutes)

1. Create a **public** GitHub repo, push this folder.
2. Settings → Secrets and variables → Actions → add:
   - `SMTP_USER` — your Gmail address
   - `SMTP_PASS` — a Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords)
   - `ALERT_TO` — where alerts go (comma-separated OK)
   - (`SMTP_HOST`/`SMTP_PORT` optional; default `smtp.gmail.com:465`)
3. Actions tab → enable workflows → run **blind-trust-pipeline** manually once
   with task `all`. First run backfills ~60 pages (~1,800 filings) politely at
   ~1 req/sec, so it takes a while; later runs finish in under a minute.
4. Point the app at the data:
   `https://raw.githubusercontent.com/<you>/<repo>/main/data/tape.json` etc.
   (raw.githubusercontent.com sends `Access-Control-Allow-Origin: *`, so the
   front end can fetch it directly — no server needed.)

## Front end (`docs/`)

`docs/index.html` is the Blind Trust app: a single self-contained page (no
build step, no dependencies) that reads the pipeline's JSON directly from
`raw.githubusercontent.com` and falls back to baked sample data when the feed
is unreachable. It has five views — Tape, Holdings, Signals, Alpha, Filings —
plus search, a filter sheet, per-filer profiles, and a "new since last visit"
notifications panel. The Tape lists 20 disclosures at a time behind a "Load
more" control and shows each stock's move since its disclosure date (green up /
amber down); "Most widely disclosed" is a proportional treemap where each
tile's area tracks its disclosure count; and avatars show official MP/Senator
headshots (from `photos.json`) inside a party-coloured ring, falling back to
initials. **Holdings** is a browsable index rather than a feed: pick a category
— Members of Parliament / Senators / Public office holders (chamber from
`roster.json`) — then sort the roster (by # securities, name, or last filing)
and tap a filer to open their full disclosures.

- **View it locally**: open `docs/index.html` in a browser, or serve the folder
  (`python3 -m http.server` from `docs/`).
- **Publish it free** with GitHub Pages: Settings → Pages → *Deploy from a
  branch* → `main` / `/docs`. The app fetches data over the absolute raw URL,
  so it works from any origin.
- **Repoint it**: the data source is the `BASE` constant near the top of the
  inline `<script>` in `docs/index.html`.

The page was generated from the `Blind Trust.dc.html` Claude Design comp; the
comp's data logic is embedded verbatim, wrapped in a tiny dependency-free
runtime (a `{{ }}`/`sc-if`/`sc-for` interpreter plus the `CompanyLogo` and
`Avatar` components) so it runs standalone. Company logos load from Clearbit at
runtime with a monogram fallback.

## Self-maintenance design

- **Incremental + idempotent**: `state.json` tracks seen declaration IDs; runs
  stop at the first fully-seen page. Safe to re-run anytime.
- **No rotting URLs**: open-government files (lobbying, contracts) are
  discovered at runtime via the CKAN API on open.canada.ca, always taking the
  freshest resource. The CIEC pagination parameter is auto-detected each run.
- **Graceful degradation**: every alt-data source fails soft (logs + empty
  output) so one broken feed never kills the registry run or the commit.
- **Polite**: identified User-Agent, 1.2 s between requests, retry/backoff on
  429/5xx.
- **Failure visibility**: if a run errors, GitHub emails you automatically
  (default Actions notification). No silent death.
- **Repo hygiene**: `registry.json` rolls at `max_records` (config.yaml) so the
  repo never balloons.

## Honesty constraints (enforced in the data model)

- The registry publishes **no amounts and no trade dates**. `tape.json` events
  are disclosure states (`held` / `added` / `removed`), never trades.
- "No longer disclosed" ≠ sold — could be sale, transfer to trust, exemption,
  or falling below the threshold. Keep the footnote in the app.
- `prices.json` measures from the **disclosure** date, not purchase date.

## SEDI

`scrapers/altdata.py::sedi()` is a documented no-op. sedi.ca has no API and a
stateful Java UI; reliable automation needs a headless browser or a vendor
feed. When you pick one, implement it there — the GFI already consumes its
output shape (`{date, ticker, insider, tx, value}`).

## Headshots & roster

`scrapers/photos.py` builds two files, both fetched server-side (in CI) so the
static app never makes a cross-origin request: `data/photos.json` (`normalized
name → photo URL`) and `data/roster.json` (`normalized name → {chamber, party,
riding}`). The roster gives the app authoritative chamber (to split the Holdings
picker into MPs / Senators / Public office holders) and party (for the avatar
ring), replacing hand-maintained overrides. The app deburrs/normalizes each filer's name the
same way and drops the matching URL into the avatar, falling back to initials
when there's no match (staff and appointees generally have none).

Sources: sitting MPs come from the Open North **Represent API**
(`represent.opennorth.ca`, which exposes a direct `photo_url`), with the House
of Commons members XML as a fallback; Senators are a best-effort scrape of the
Senate of Canada listing. Every source is isolated and fail-soft, so one
failing never drops the others or breaks the run. The photos are Parliament of
Canada material — keep the "not a Government of Canada site" footer and treat
them as attributed public images, not your own.

Photos refresh on the nightly `all` run; trigger an immediate refresh by
dispatching the workflow with task `photos`.

## Extending the ticker map

Unmatched security names show up with `"ticker": null` in `holdings.json`.
Add a name fragment → ticker line to `config.yaml`; next run picks it up.

## Legal / etiquette

All sources are public data. The crawler is rate-limited and identified.
Respect the registry's terms; this is a read-only mirror for accountability
purposes, not a bulk redistribution service.
