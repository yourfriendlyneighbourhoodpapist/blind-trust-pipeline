"""CIEC public-registry scraper.

Walks https://ciec-ccie.parl.gc.ca/en/public-registry newest-first and stops
once a full page contains only declaration IDs we've already seen (state.json).
Each entry becomes a normalized record:

{
  "id": "<declarationId GUID>",
  "doc": "Notice of Material Change",
  "person": "Roman Baber",
  "client_id": "<clientId GUID>",
  "role": "Member of Parliament",
  "regime": "Conflict of Interest Code for Members of the House of Commons",
  "type": "Material Changes",
  "disclosed": "2026-07-16",
  "url": "https://ciec-ccie.parl.gc.ca/en/public-registry/Details?declarationId=...",
  "fields": [{"label": "Description", "value": "..."}],
}
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .common import log, polite_get, session

BASE = "https://ciec-ccie.parl.gc.ca"
LIST_URL = f"{BASE}/en/public-registry"

DECL_TYPES = [
    "Compliance Measures", "Declarable Assets", "Disclosure Summaries (Code)",
    "Forfeited Gifts (Act)", "Gifts (Act)", "Gifts (Code)", "Liabilities",
    "Material Changes", "Other Appropriate Documents", "Outside Activities",
    "Private Interest", "Recusals", "Sponsored Travel", "Summary Statements (Act)",
    "Travel", "Compliance Orders", "Examinations (Act)", "Exemptions (Section 38)",
    "Inquiries (Code)", "Penalties", "Waivers (Section 39)",
]
REGIMES = [
    "Conflict of Interest Code for Members of the House of Commons",
    "Conflict of Interest Act",
]
FIELD_LABELS = [
    "Description", "Nature", "Source", "Circumstance", "Gift received date",
    "Destination", "Sponsor", "Purpose", "Dates", "Violation", "Penalty",
    "Recusal", "Subject",
]
_DISCLOSED_RE = re.compile(r"Disclosed on\s+(\d{4}-\d{2}-\d{2})")
_PAGE_PARAMS = ("page", "pageNumber", "p")  # first that changes results wins


def _guid(href: str, key: str) -> str | None:
    qs = parse_qs(urlparse(href).query)
    return (qs.get(key) or [None])[0]


def _entry_container(a_tag):
    """Climb from the Details link to the smallest ancestor holding the whole
    entry (person link + 'Disclosed on' date)."""
    node = a_tag
    for _ in range(8):
        node = node.parent
        if node is None:
            return a_tag.parent
        text = node.get_text(" ", strip=True)
        if "Disclosed on" in text and node.find("a", href=re.compile(r"clientId=")):
            return node
    return a_tag.parent


def _parse_entry(container, detail_link) -> dict | None:
    text = container.get_text("\n", strip=True)
    m = _DISCLOSED_RE.search(text)
    if not m:
        return None
    decl_id = _guid(detail_link["href"], "declarationId")
    person_a = container.find("a", href=re.compile(r"clientId="))
    person = person_a.get_text(strip=True) if person_a else "Unknown"
    client_id = _guid(person_a["href"], "clientId") if person_a else None

    # role: text immediately following the person link, "· Member of Parliament"
    role = ""
    if person_a is not None:
        tail = person_a.parent.get_text(" ", strip=True)
        rm = re.search(re.escape(person) + r"\s*·\s*([^·\n]+)", tail)
        if rm:
            role = rm.group(1).strip()

    regime = next((r for r in REGIMES if r in text), "")
    dtype = ""
    for t in DECL_TYPES:  # last match before the date line is the type tag
        if re.search(re.escape(t), text):
            dtype = t if not dtype or text.rfind(t) > text.rfind(dtype) else dtype
            dtype = dtype or t

    # fields: split body on known labels
    fields = []
    lines = [ln for ln in text.split("\n") if ln.strip()]
    current = None
    for ln in lines:
        stripped = ln.strip()
        if stripped in FIELD_LABELS:
            current = {"label": stripped, "value": ""}
            fields.append(current)
        elif stripped in ("Show more", "View rule", "View attachment") or _DISCLOSED_RE.match(stripped):
            current = None
        elif stripped in DECL_TYPES or stripped in REGIMES:
            current = None
        elif current is not None:
            current["value"] = (current["value"] + "\n" + stripped).strip()

    return {
        "id": decl_id,
        "doc": detail_link.get_text(strip=True),
        "person": person,
        "client_id": client_id,
        "role": role,
        "regime": regime,
        "type": dtype,
        "disclosed": m.group(1),
        "url": urljoin(BASE, detail_link["href"]),
        "fields": [f for f in fields if f["value"]],
    }


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"Details\?declarationId=")):
        decl_id = _guid(a["href"], "declarationId")
        if not decl_id or decl_id in seen:
            continue
        seen.add(decl_id)
        rec = _parse_entry(_entry_container(a), a)
        if rec:
            out.append(rec)
    return out


def _detect_page_param(sess) -> str:
    first = _parse_page(polite_get(sess, LIST_URL).text)
    first_ids = {r["id"] for r in first}
    for param in _PAGE_PARAMS:
        try:
            page2 = _parse_page(polite_get(sess, LIST_URL, params={param: 2}).text)
        except Exception:  # noqa: BLE001
            continue
        if page2 and {r["id"] for r in page2} != first_ids:
            log.info("Pagination param detected: %s", param)
            return param
    log.warning("Could not detect pagination param; defaulting to 'page'")
    return "page"


def scrape(seen_ids: set[str], max_pages: int = 40) -> list[dict]:
    """Return records not in seen_ids, newest first. First run: bounded
    backfill of max_pages; later runs stop at the first fully-seen page."""
    sess = session()
    param = _detect_page_param(sess)
    new_records: list[dict] = []
    for page in range(1, max_pages + 1):
        html = polite_get(sess, LIST_URL, params={param: page} if page > 1 else None).text
        records = _parse_page(html)
        if not records:
            log.info("Page %d empty — stopping", page)
            break
        fresh = [r for r in records if r["id"] not in seen_ids]
        new_records.extend(fresh)
        log.info("Page %d: %d entries, %d new", page, len(records), len(fresh))
        if not fresh and seen_ids:
            break  # caught up
    return new_records
