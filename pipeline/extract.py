"""Turn raw registry records into structured holdings, tape events, and
matches. Heuristics are conservative: anything ambiguous stays in
'other assets' and raw text is always preserved on the record."""
from __future__ import annotations

import re

SECURITIES_TYPES = {
    "Disclosure Summaries (Code)", "Material Changes", "Declarable Assets",
    "Compliance Measures", "Summary Statements (Act)", "Liabilities",
}

_SEC_LINE = re.compile(
    r"^(?:-\s*)?(?:Shares?|Units?|Stock options?|Bonds?|Debentures?|Notes?)\s+(?:of|in)\s+(.{2,90})$",
    re.I)
_SEC_GENERIC = re.compile(r"publicly traded securities", re.I)
_NOT_SECURITY = re.compile(
    r"(mutual fund|index fund|etf|exchange[- ]traded)", re.I)  # funds: keep but flag
_OTHER_HINT = re.compile(
    r"(trust|fiducie|mortgage|line of credit|credit card|rental|residential|real property|"
    r"private corporation|sole ownership|guarantor|blind trust|screen)", re.I)

# material-change verbs
_ADDED = re.compile(r"\b(acquired|purchased|now (?:hold|own)|now requires? public disclosure)\b", re.I)
_REMOVED = re.compile(
    r"\b(sold|disposed of|divested|no longer (?:hold|own|requires? public disclosure))\b", re.I)


def _clean(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" .;,")
    name = re.sub(r"\s*\((?:spouse|common-law partner)[^)]*\)\s*$", "", name, flags=re.I)
    return name


def split_assets(description: str, spouse_section: bool = False) -> tuple[list[str], list[str]]:
    """Return (securities, other) from a Description blob. Tracks the
    spouse's-section context so holdings get the ' (spouse)' suffix."""
    securities, other = [], []
    in_spouse = spouse_section
    for raw in description.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if re.match(r"Spouse's/Common-Law Partner's", line, re.I):
            in_spouse = True
            continue
        if re.match(r"^(Assets|Liabilities|Activities|Trusts|Other Sources of Income|"
                    r"Investment in Private Corporations)$", line, re.I):
            in_spouse = in_spouse and line.lower().startswith("spouse")
            continue
        suffix = " (spouse)" if in_spouse else ""
        m = _SEC_LINE.match(line)
        if m and not _OTHER_HINT.search(line):
            securities.append(_clean(m.group(1)) + suffix)
        elif _SEC_GENERIC.search(line):
            securities.append("Publicly traded securities (not itemized)" + suffix)
        elif _OTHER_HINT.search(line) or line.startswith("-"):
            other.append(line.lstrip("- ").strip() + suffix)
    return securities, other


def parse_material_change(description: str) -> dict:
    """Extract added/removed securities from a Notice of Material Change."""
    added, removed = [], []
    in_assets = False
    for raw in description.split("\n"):
        line = raw.strip()
        if re.match(r"^[A-Z\s]+\[Paragraph", line):
            in_assets = line.upper().startswith("ASSETS")
            continue
        if not line:
            continue
        m = _SEC_LINE.match(re.sub(r"^I (?:acquired|purchased|sold|disposed of|divested)\s+", "", line, flags=re.I))
        names = [_clean(m.group(1))] if m else []
        if not names:
            # list-style changes: "now require public disclosure: A, B, C"
            lm = re.match(r".*?public disclosure[:\s]+(.+)$", line, re.I)
            if lm:
                names = [_clean(n) for n in re.split(r"[;,]| and ", lm.group(1)) if _clean(n)]
        if not names or not in_assets and not (_ADDED.search(line) or _REMOVED.search(line)):
            # still allow explicit verbs outside a section header
            if not names:
                continue
        if _REMOVED.search(line):
            removed.extend(names)
        elif _ADDED.search(line):
            added.extend(names)
    return {"added": added, "removed": removed}


def enrich(records: list[dict]) -> None:
    """Mutates records in place, attaching securities/otherAssets/changes."""
    for r in records:
        desc = "\n".join(f["value"] for f in r.get("fields", [])
                         if f["label"] in ("Description", "Nature"))
        if r.get("type") == "Material Changes":
            r["changes"] = parse_material_change(desc)
            sec, other = split_assets(desc)
            r["securities"], r["otherAssets"] = sec, other
        elif r.get("type") in SECURITIES_TYPES:
            sec, other = split_assets(desc)
            r["securities"], r["otherAssets"] = sec, other
        else:
            r["securities"], r["otherAssets"] = [], []
        r["is_securities"] = bool(
            r.get("type") in SECURITIES_TYPES
            and (r["securities"] or r.get("changes", {}).get("added")
                 or r.get("changes", {}).get("removed")
                 or r.get("type") in ("Declarable Assets", "Compliance Measures")))


def tape_events(records: list[dict]) -> list[dict]:
    rows = []
    for r in records:
        base = {"person": r["person"], "date": r["disclosed"], "url": r.get("url", "")}
        for s in r.get("securities", []):
            rows.append({**base, "s": s, "ev": "held"})
        ch = r.get("changes") or {}
        for s in ch.get("added", []):
            rows.append({**base, "s": s, "ev": "added"})
        for s in ch.get("removed", []):
            rows.append({**base, "s": s, "ev": "removed"})
    rows.sort(key=lambda x: (x["date"], x["person"]), reverse=True)
    return rows


def match_ticker(name: str, ticker_map: dict[str, str]) -> str | None:
    """ticker_map keys are lowercase company-name fragments."""
    n = re.sub(r"\s*\(spouse\)\s*$", "", name).lower()
    n = re.sub(r"\b(inc|corp|corporation|ltd|limited|plc|co|company)\.?\b", "", n).strip(" .,")
    for frag, tkr in ticker_map.items():
        if frag in n or n in frag:
            return tkr
    return None


def favour_index(signals, lobby, contracts, sedi, holders_by_ticker) -> list[dict]:
    """Composite Government Favour Index per ticker. Transparent weights;
    every component is listed so the app can footnote the math."""
    tickers = set(holders_by_ticker)
    for coll, key in ((signals, "companies"), (lobby, None), (contracts, None), (sedi, None)):
        for item in coll:
            if key:
                tickers.update(item.get(key, []))
            elif item.get("ticker"):
                tickers.add(item["ticker"])
    out = []
    lob = {r["ticker"]: r for r in lobby}
    for tkr in tickers:
        ann = sum(1 for s in signals if tkr in s.get("companies", []))
        con = [c for c in contracts if c.get("ticker") == tkr]
        comms = lob.get(tkr, {}).get("comms", 0)
        buys = sum(1 for s in sedi if s.get("ticker") == tkr and s.get("tx") == "Buy")
        sells = sum(1 for s in sedi if s.get("ticker") == tkr and s.get("tx") == "Sell")
        holders = len(holders_by_ticker.get(tkr, []))
        score = min(100, ann * 20 + len(con) * 15 + min(comms, 50) + (buys - sells) * 5 + holders * 5)
        if score <= 0:
            continue
        parts = []
        if ann: parts.append(f"{ann} announcement{'s' if ann != 1 else ''}")
        if con: parts.append(f"{len(con)} contract{'s' if len(con) != 1 else ''}")
        if comms: parts.append(f"{comms} comms")
        if buys or sells: parts.append(f"insider {'buying' if buys >= sells else 'selling'}")
        if holders: parts.append(f"held by {holders} filer{'s' if holders != 1 else ''}")
        out.append({"ticker": tkr, "score": score, "parts": " · ".join(parts)})
    out.sort(key=lambda r: -r["score"])
    return out
