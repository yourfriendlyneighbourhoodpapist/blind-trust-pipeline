"""Alt-data scrapers: Canada.ca newsroom, Registry of Lobbyists,
CanadaBuys contract awards, SEDI (best-effort), and market prices.

Design rule for self-maintenance: never hard-code file URLs that rot.
Open-government datasets are discovered at runtime through the CKAN API on
open.canada.ca, then the freshest CSV resource is downloaded.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime, timedelta

from .common import log, polite_get, session

# --------------------------------------------------------------------------
# 1. Canada.ca newsroom — press releases naming tracked public companies
# --------------------------------------------------------------------------
NEWS_API = "https://api.io.canada.ca/io-server/gc/news/en/v2"


def _news_page(sess, pick: int, page: int) -> list[dict]:
    resp = polite_get(sess, NEWS_API, params={
        "experience": "products", "sort": "publishedDate", "orderBy": "desc",
        "pick": pick, "page": page, "format": "json"})
    return resp.json().get("feed", {}).get("entry", []) or []


def newsroom(company_terms: dict[str, str], days: int = 30, since: str | None = None,
             pick: int = 500, max_pages: int = 40) -> list[dict]:
    """company_terms: {search term -> ticker}. Returns releases whose
    title/teaser name a tracked company, scanning back to ``since`` (YYYY-MM-DD)
    or ``days`` ago. Pages the feed until it passes the cutoff; if the API does
    not paginate, a repeated page yields no new links and the loop stops
    (best-effort — releases naming a specific company are inherently sparse).
    Companies are emitted as [{name, ticker}] for display + scoring."""
    cutoff = since or (date.today() - timedelta(days=days)).isoformat()
    sess = session()
    items: list[dict] = []
    seen_links: set = set()
    for page in range(1, max_pages + 1):
        try:
            batch = _news_page(sess, pick, page)
        except Exception as exc:  # noqa: BLE001
            if page > 1:
                break  # got some pages already; stop on later-page error
            log.warning("Newsroom API failed (%s); trying Atom fallback", exc)
            try:
                import feedparser
                feed = feedparser.parse(
                    f"{NEWS_API}?experience=products&sort=publishedDate&orderBy=desc&pick={pick}&format=atom")
                batch = [{"title": e.get("title", ""), "teaser": e.get("summary", ""),
                          "publishedDate": e.get("published", ""), "link": e.get("link", ""),
                          "department": e.get("author", "")} for e in feed.entries]
            except Exception as exc2:  # noqa: BLE001
                log.error("Newsroom fallback failed: %s", exc2)
                return []
        fresh = [it for it in batch if (it.get("link") or repr(it)) not in seen_links]
        if not fresh:
            break  # no pagination support / exhausted
        for it in fresh:
            seen_links.add(it.get("link") or repr(it))
        items.extend(fresh)
        oldest = min(((it.get("publishedDate") or "")[:10] for it in fresh
                      if it.get("publishedDate")), default="")
        if oldest and oldest < cutoff:
            break

    hits = []
    for it in items:
        title = it.get("title", "") or ""
        teaser = it.get("teaser", "") or it.get("summary", "") or ""
        blob = f"{title} {teaser}"
        matched = {tkr: term for term, tkr in company_terms.items()
                   if re.search(rf"\b{re.escape(term)}\b", blob, re.I)}
        pub = (it.get("publishedDate") or "")[:10]
        if matched and pub >= cutoff:
            hits.append({
                "date": pub, "title": title.strip(),
                "source": (it.get("department") or it.get("deptAcronym") or "Government of Canada"),
                "url": it.get("link", ""), "teaser": teaser.strip()[:400], "sector": "Announcement",
                "companies": [{"name": term, "ticker": tkr} for tkr, term in sorted(matched.items())],
            })
    log.info("Newsroom: scanned %d releases since %s, %d matches", len(items), cutoff, len(hits))
    return hits


# --------------------------------------------------------------------------
# CKAN discovery helper (open.canada.ca)
# --------------------------------------------------------------------------
CKAN = "https://open.canada.ca/data/api/action/package_search"


def _ckan_latest_resource(query: str, fmt_pref=("CSV", "ZIP")) -> str | None:
    sess = session()
    try:
        resp = polite_get(sess, CKAN, params={"q": query, "rows": 5})
        results = resp.json()["result"]["results"]
    except Exception as exc:  # noqa: BLE001
        log.error("CKAN search failed for %r: %s", query, exc)
        return None
    for pkg in results:
        for fmt in fmt_pref:
            for res in pkg.get("resources", []):
                if (res.get("format") or "").upper() == fmt and res.get("url"):
                    log.info("CKAN %r -> %s", query, res["url"])
                    return res["url"]
    return None


# --------------------------------------------------------------------------
# 2. Registry of Lobbyists — communication reports, count per tracked company
# --------------------------------------------------------------------------
def lobbying(company_terms: dict[str, str], days: int = 90) -> list[dict]:
    url = _ckan_latest_resource("lobbying communication reports registry of lobbyists")
    if not url:
        return []
    sess = session()
    try:
        raw = polite_get(sess, url).content
    except Exception as exc:  # noqa: BLE001
        log.error("Lobbying download failed: %s", exc)
        return []
    if url.lower().endswith(".zip") or raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
            if not name:
                return []
            raw = zf.read(name)

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    counts: dict[str, dict] = {}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    cols = {c.lower(): c for c in (reader.fieldnames or [])}
    client_col = next((cols[c] for c in cols if "client" in c and "name" in c), None)
    date_col = next((cols[c] for c in cols if "date" in c), None)
    inst_col = next((cols[c] for c in cols if "institution" in c), None)
    if not client_col:
        log.error("Lobbying CSV schema unrecognized: %s", reader.fieldnames)
        return []
    for row in reader:
        client = (row.get(client_col) or "").strip()
        when = (row.get(date_col) or "")[:10] if date_col else ""
        if when and when < cutoff:
            continue
        for term, tkr in company_terms.items():
            if re.search(rf"\b{re.escape(term)}\b", client, re.I):
                slot = counts.setdefault(tkr, {"ticker": tkr, "co": client,
                                               "comms": 0, "institutions": {}})
                slot["comms"] += 1
                inst = (row.get(inst_col) or "").strip() if inst_col else ""
                if inst:
                    slot["institutions"][inst] = slot["institutions"].get(inst, 0) + 1
    out = []
    for slot in counts.values():
        top = sorted(slot["institutions"], key=slot["institutions"].get, reverse=True)[:3]
        out.append({"ticker": slot["ticker"], "co": slot["co"],
                    "comms": slot["comms"], "topInst": ", ".join(top)})
    out.sort(key=lambda r: -r["comms"])
    log.info("Lobbying: %d tracked companies with comms", len(out))
    return out


# --------------------------------------------------------------------------
# 3. CanadaBuys / proactive disclosure — contract awards to tracked companies
# --------------------------------------------------------------------------
def contracts(company_terms: dict[str, str], days: int = 120) -> list[dict]:
    url = _ckan_latest_resource("proactive disclosure contracts over 10000")
    if not url:
        return []
    sess = session()
    try:
        raw = polite_get(sess, url).content
    except Exception as exc:  # noqa: BLE001
        log.error("Contracts download failed: %s", exc)
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = []
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    cols = {c.lower(): c for c in (reader.fieldnames or [])}
    vend = next((cols[c] for c in cols if "vendor" in c), None)
    val = next((cols[c] for c in cols if "value" in c or "amount" in c), None)
    dt = next((cols[c] for c in cols if "date" in c), None)
    dept = next((cols[c] for c in cols if "owner" in c or "organization" in c or "department" in c), None)
    desc = next((cols[c] for c in cols if "description" in c), None)
    if not vend:
        log.error("Contracts CSV schema unrecognized: %s", reader.fieldnames)
        return []
    for row in reader:
        vendor = (row.get(vend) or "").strip()
        when = (row.get(dt) or "")[:10] if dt else ""
        if when and when < cutoff:
            continue
        for term, tkr in company_terms.items():
            if re.search(rf"\b{re.escape(term)}\b", vendor, re.I):
                out.append({"date": when, "co": vendor, "ticker": tkr,
                            "dept": (row.get(dept) or "").strip() if dept else "",
                            "desc": ((row.get(desc) or "").strip()[:140]) if desc else "",
                            "value": (row.get(val) or "").strip() if val else ""})
    out.sort(key=lambda r: r["date"], reverse=True)
    log.info("Contracts: %d awards to tracked companies", len(out))
    return out[:100]


# --------------------------------------------------------------------------
# 4. SEDI — best effort. sedi.ca has no API and a session-based UI.
# --------------------------------------------------------------------------
def sedi(tickers: list[str]) -> list[dict]:
    """SEDI (sedi.ca) publishes insider filings behind a stateful Java web UI
    with no stable public endpoint. Reliable automation requires either a
    headless browser or a commercial feed. This function is a documented
    placeholder that returns [] and logs the gap so the pipeline never breaks;
    swap in a vendor feed (e.g. via an API key in secrets) when ready."""
    log.warning("SEDI: no automated source configured (%d tickers tracked); "
                "returning empty set. See README §SEDI.", len(tickers))
    return []


# --------------------------------------------------------------------------
# 5. Prices — % change since disclosure date (yfinance)
# --------------------------------------------------------------------------
def prices(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """pairs: [(ticker, disclosed YYYY-MM-DD)]. Returns
    {ticker: {"since": date, "pct": float, "asOf": today}}."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed")
        return {}
    out, today = {}, date.today().isoformat()
    wanted: dict[str, str] = {}
    for tkr, disclosed in pairs:  # earliest disclosure wins per ticker
        if tkr and (tkr not in wanted or disclosed < wanted[tkr]):
            wanted[tkr] = disclosed
    for tkr, disclosed in wanted.items():
        try:
            hist = yf.Ticker(tkr).history(
                start=disclosed,
                end=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
                auto_adjust=True)
            if len(hist) >= 2:
                pct = (hist["Close"].iloc[-1] / hist["Close"].iloc[0] - 1) * 100
                out[tkr] = {"since": disclosed, "pct": round(float(pct), 2), "asOf": today}
        except Exception as exc:  # noqa: BLE001
            log.warning("Price fetch failed for %s: %s", tkr, exc)
    log.info("Prices: %d/%d tickers priced", len(out), len(wanted))
    return out
