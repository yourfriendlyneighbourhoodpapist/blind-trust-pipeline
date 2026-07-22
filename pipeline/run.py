"""Orchestrator. Subcommands:

  python -m pipeline.run registry   # CIEC scrape + diff + enrich + alert (every 6h)
  python -m pipeline.run altdata    # newsroom + lobbying + contracts + sedi (daily)
  python -m pipeline.run prices     # perf since disclosure (daily)
  python -m pipeline.run all

Outputs (data/): state.json, registry.json, tape.json, holdings.json,
signals.json, alpha.json, prices.json — the app reads these as raw JSON
from the repo (raw.githubusercontent.com serves CORS-friendly responses).
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from scrapers import altdata, ciec  # noqa: E402
from scrapers.common import load_json, log, save_json  # noqa: E402
from pipeline import alerts, extract  # noqa: E402

CONFIG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
TICKER_MAP = {k.lower(): v for k, v in (CONFIG.get("tickers") or {}).items()}
COMPANY_TERMS = {k: v for k, v in (CONFIG.get("tickers") or {}).items()}
MAX_RECORDS = int(CONFIG.get("max_records", 4000))


def _holders_by_ticker(records):
    out = defaultdict(set)
    for r in records:
        for s in r.get("securities", []):
            tkr = extract.match_ticker(s, TICKER_MAP)
            if tkr:
                out[tkr].add(r["person"])
    return {k: sorted(v) for k, v in out.items()}


SINCE_DATE = CONFIG.get("since_date") or None


def run_registry(force_backfill: bool = False) -> None:
    state = load_json("state.json", {"seen_ids": [], "backfilled": False})
    seen = set(state["seen_ids"])

    # A deep sweep runs on the very first backfill, whenever a full window pull
    # was never completed, when since_date changes, or on an explicit request.
    need_deep = (force_backfill or not state.get("since_backfilled")
                 or state.get("since_date") != SINCE_DATE)
    if need_deep:
        max_pages = int(CONFIG.get("deep_backfill_pages", 400))
        log.info("Deep backfill: sweeping disclosures since %s, up to %d pages",
                 SINCE_DATE, max_pages)
    else:
        max_pages = int(CONFIG.get("backfill_pages", 40)) if not state["backfilled"] else 15
    new = ciec.scrape(seen, max_pages=max_pages, since_date=SINCE_DATE, deep=need_deep)
    extract.enrich(new)

    # Merge with the existing store, de-duplicating by declaration id (a deep
    # sweep re-returns records we already have); freshly enriched copies win.
    registry = load_json("registry.json", [])
    by_id: dict = {}
    for r in new + registry:
        rid = r.get("id")
        if rid and rid not in by_id:
            by_id[rid] = r
    registry = list(by_id.values())
    if SINCE_DATE:  # enforce the window even on records carried over from before
        registry = [r for r in registry if r.get("disclosed", "") >= SINCE_DATE]
    registry.sort(key=lambda r: r.get("disclosed", ""), reverse=True)  # newest first
    registry = registry[:MAX_RECORDS]
    save_json("registry.json", registry)
    save_json("tape.json", extract.tape_events(registry))

    # per-person holdings ledger
    people = {}
    for r in registry:
        p = people.setdefault(r["person"], {
            "person": r["person"], "client_id": r.get("client_id"),
            "role": r.get("role", ""), "records": 0, "lastFiling": "",
            "securities": {}, "otherAssets": [], "added": [], "removed": []})
        p["records"] += 1
        p["lastFiling"] = max(p["lastFiling"], r["disclosed"])
        for s in r.get("securities", []):
            if s not in p["securities"] or r["disclosed"] > p["securities"][s]:
                p["securities"][s] = r["disclosed"]
        for s in r.get("otherAssets", []):
            if s not in p["otherAssets"]:
                p["otherAssets"].append(s)
        ch = r.get("changes") or {}
        p["added"] += [{"s": s, "date": r["disclosed"]} for s in ch.get("added", [])]
        p["removed"] += [{"s": s, "date": r["disclosed"]} for s in ch.get("removed", [])]
    for p in people.values():
        p["securities"] = [{"s": s, "date": d, "ticker": extract.match_ticker(s, TICKER_MAP)}
                           for s, d in sorted(p["securities"].items())]
    save_json("holdings.json", sorted(people.values(),
                                      key=lambda p: p["lastFiling"], reverse=True))

    state["seen_ids"] = list({r["id"] for r in registry} | seen)[-50000:]
    state["backfilled"] = True
    state["since_backfilled"] = True
    state["since_date"] = SINCE_DATE
    save_json("state.json", state)

    # Don't email on a backfill (first run or deep sweep) — only on incremental
    # runs that surface genuinely new declarations.
    if new and seen and not need_deep:
        alerts.send_new_declarations(new)
    log.info("Registry run complete: %d fetched, %d total (deep=%s)",
             len(new), len(registry), need_deep)


def _window_days() -> int:
    """Length of the scan window: from `since_date` to today, or `news_days`."""
    if SINCE_DATE:
        try:
            from datetime import date as _date
            y, m, d = (int(x) for x in SINCE_DATE.split("-"))
            return (_date.today() - _date(y, m, d)).days + 1
        except Exception:  # noqa: BLE001
            pass
    return int(CONFIG.get("news_days", 45))


def run_altdata() -> None:
    registry = load_json("registry.json", [])
    holders = _holders_by_ticker(registry)
    win = _window_days()

    # Government releases naming a tracked company, scanned back to since_date.
    signals = altdata.newsroom(COMPANY_TERMS, days=win, since=SINCE_DATE)
    lobby = altdata.lobbying(COMPANY_TERMS, days=win)
    contracts = altdata.contracts(COMPANY_TERMS, days=win)
    sedi = altdata.sedi(sorted(set(TICKER_MAP.values())))

    # Enrich sparse newsroom hits with contract awards + lobbying spikes that
    # name a disclosed-held company, so the Signals tab is substantive.
    signals += extract.derived_signals(contracts, lobby, holders)
    for s in signals:  # attach the disclosed filers who hold a named company
        tickers = extract._sig_tickers(s)
        s["holders"] = sorted({h for t in tickers for h in holders.get(t, [])})
    # de-dup by (date, title) and present newest first
    seen, uniq = set(), []
    for s in sorted(signals, key=lambda x: x.get("date", ""), reverse=True):
        key = (s.get("date", ""), s.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    signals = uniq
    save_json("signals.json", signals)

    gfi = extract.favour_index(signals, lobby, contracts, sedi, holders)
    save_json("alpha.json", {"gfi": gfi, "sedi": sedi, "lobby": lobby,
                             "contracts": contracts})
    log.info("Altdata run complete: %d signals, %d lobby rows, %d contracts",
             len(signals), len(lobby), len(contracts))


def run_prices() -> None:
    registry = load_json("registry.json", [])
    pairs = []
    for r in registry:
        for s in r.get("securities", []):
            tkr = extract.match_ticker(s, TICKER_MAP)
            if tkr:
                pairs.append((tkr, r["disclosed"]))
    save_json("prices.json", altdata.prices(pairs))


def run_photos() -> None:
    """Refresh data/photos.json (name -> headshot URL) and data/roster.json
    (name -> {chamber, party, riding}). Fail-soft: if a source errors we keep
    whatever we already had, so the app degrades gracefully."""
    from scrapers import photos
    old_photos = load_json("photos.json", {})
    old_roster = load_json("roster.json", {})
    fresh_photos, fresh_roster = photos.fetch()
    if fresh_photos:
        save_json("photos.json", {**old_photos, **fresh_photos})
    else:
        log.warning("Photos: nothing fetched; keeping existing %d", len(old_photos))
    if fresh_roster:
        save_json("roster.json", {**old_roster, **fresh_roster})
    else:
        log.warning("Roster: nothing fetched; keeping existing %d", len(old_roster))
    log.info("Photos/roster: %d photos, %d roster entries this run",
             len(fresh_photos), len(fresh_roster))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task",
                    choices=["registry", "altdata", "prices", "photos", "all", "backfill"])
    task = ap.parse_args().task
    if task == "backfill":
        run_registry(force_backfill=True)
    if task in ("registry", "all"):
        run_registry()
    if task in ("altdata", "all"):
        run_altdata()
    if task in ("prices", "all"):
        run_prices()
    if task in ("photos", "all"):
        run_photos()


if __name__ == "__main__":
    main()
