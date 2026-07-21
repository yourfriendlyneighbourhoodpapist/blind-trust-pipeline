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


def run_registry() -> None:
    state = load_json("state.json", {"seen_ids": [], "backfilled": False})
    seen = set(state["seen_ids"])
    max_pages = int(CONFIG.get("backfill_pages", 40)) if not state["backfilled"] else 15
    new = ciec.scrape(seen, max_pages=max_pages)
    extract.enrich(new)

    registry = load_json("registry.json", [])
    registry = new + registry
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
    save_json("state.json", state)

    if new and seen:  # don't spam on the very first backfill
        alerts.send_new_declarations(new)
    log.info("Registry run complete: %d new, %d total", len(new), len(registry))


def run_altdata() -> None:
    registry = load_json("registry.json", [])
    signals = altdata.newsroom(COMPANY_TERMS, days=int(CONFIG.get("news_days", 45)))
    # attach disclosed holders to each announcement
    holders = _holders_by_ticker(registry)
    for s in signals:
        s["holders"] = sorted({h for t in s["companies"] for h in holders.get(t, [])})
    save_json("signals.json", signals)

    lobby = altdata.lobbying(COMPANY_TERMS, days=90)
    contracts = altdata.contracts(COMPANY_TERMS, days=120)
    sedi = altdata.sedi(sorted(set(TICKER_MAP.values())))
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["registry", "altdata", "prices", "all"])
    task = ap.parse_args().task
    if task in ("registry", "all"):
        run_registry()
    if task in ("altdata", "all"):
        run_altdata()
    if task in ("prices", "all"):
        run_prices()


if __name__ == "__main__":
    main()
