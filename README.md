# Blind Trust — data pipeline

Self-maintaining backend for the Blind Trust app. GitHub Actions runs the
scrapers on a schedule, commits fresh JSON to `data/`, and emails you when new
CIEC filings appear. Zero servers, zero cost on a public repo.

## What runs when

| Task | Schedule | What it does |
|---|---|---|
| `registry` | every 6 h | Scrapes the CIEC public registry newest-first, stops at the first already-seen page, extracts securities + material-change diffs, emails alerts |
| `altdata` | nightly | Canada.ca newsroom (announcement matching), Registry of Lobbyists comms counts, CanadaBuys contract awards, Government Favour Index |
| `prices` | nightly | % change since disclosure date per matched ticker (yfinance) |

Outputs in `data/`: `registry.json`, `tape.json`, `holdings.json`,
`signals.json`, `alpha.json`, `prices.json`, `state.json`.

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

## Extending the ticker map

Unmatched security names show up with `"ticker": null` in `holdings.json`.
Add a name fragment → ticker line to `config.yaml`; next run picks it up.

## Legal / etiquette

All sources are public data. The crawler is rate-limited and identified.
Respect the registry's terms; this is a read-only mirror for accountability
purposes, not a bulk redistribution service.
