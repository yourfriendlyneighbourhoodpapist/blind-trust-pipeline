"""Headshots for MPs and Senators -> data/photos.json.

Produces a flat map ``{normalized_name: photo_url}``. Fetched server-side (in
CI), so the static app never makes a cross-origin request for these — it just
reads the committed JSON and drops each URL into an <img>. Every source is
best-effort and fail-soft: if one errors it contributes nothing and the run
keeps whatever the others (and the previous file) had, so the app degrades to
initials rather than breaking.

Name matching: the app only knows a person's display name (e.g. "Mark Carney",
"François-Philippe Champagne"). Both sides normalize names identically —
deburr accents, lowercase, and collapse to single-spaced alphanumerics — so
"Fayçal El-Khoury" -> "faycal el khoury" on the scraper and in the browser.
Keep ``_pnorm`` here in sync with ``_pnorm`` in docs/index.html.
"""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .common import log, polite_get, session

# House of Commons via Open North's Represent API — stable JSON with a direct
# photo_url per sitting member. Primary MP source.
REPRESENT = "https://represent.opennorth.ca/representatives/house-of-commons/"
# Fallback: the House of Commons members list as XML.
OURCOMMONS_XML = "https://www.ourcommons.ca/members/en/search?output=XML"
OFFICIAL_MP_PHOTO = ("https://www.ourcommons.ca/Content/Parliamentarians/Images/"
                     "OfficialMPPhotos/45/{last}{first}_{party}.jpg")
# Senate of Canada — current senators listing (best-effort HTML scrape).
SENATE_LIST = "https://sencanada.ca/en/senators/"
SENATE_BASE = "https://sencanada.ca"

_HONORIFIC = re.compile(
    r"^\s*(the\s+)?(right\s+|rt\s+)?hon(ourable|orable|\.)?\s+|^\s*(mr|mrs|ms|dr|sen|senator)\.?\s+",
    re.I)


def _pnorm(name: str) -> str:
    """deburr + lowercase + single-spaced alphanumerics. Mirror of the app's
    _pnorm. Honorifics are stripped from the front only (Senate listings carry
    'The Honourable …'); registry names are already plain."""
    s = _HONORIFIC.sub("", name or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _mps_represent(sess) -> dict[str, str]:
    out: dict[str, str] = {}
    offset, limit = 0, 100
    for _ in range(20):  # safety cap (~2000 members max)
        data = polite_get(sess, REPRESENT,
                          params={"limit": limit, "offset": offset}).json()
        objects = data.get("objects", [])
        for o in objects:
            name, photo = o.get("name"), o.get("photo_url")
            if name and photo:
                out[_pnorm(name)] = photo
        if len(objects) < limit:
            break
        offset += limit
    log.info("Photos/MPs (Represent): %d", len(out))
    return out


def _mps_ourcommons(sess) -> dict[str, str]:
    """Fallback: derive names + a constructed OfficialMPPhotos URL from the
    members XML. The filename pattern is best-effort; broken URLs fail over to
    initials in the app."""
    root = ET.fromstring(polite_get(sess, OURCOMMONS_XML).content)
    party_abbr = {"liberal": "Lib", "conservative": "CPC", "ndp": "NDP",
                  "new democratic party": "NDP", "bloc québécois": "BQ",
                  "bloc quebecois": "BQ", "green party": "GP", "green": "GP"}
    out: dict[str, str] = {}
    for m in root.iter():
        if not m.tag.lower().endswith("memberofparliament"):
            continue

        def g(*tags):
            for t in tags:
                el = m.find(t)
                if el is not None and el.text:
                    return el.text.strip()
            return ""

        first = g("PersonOfficialFirstName", "PersonShortHonorific")
        last = g("PersonOfficialLastName")
        caucus = g("CaucusShortName")
        if not (first and last):
            continue
        clean = lambda s: re.sub(r"[^A-Za-z]", "",
                                 unicodedata.normalize("NFKD", s)
                                 .encode("ascii", "ignore").decode())
        url = OFFICIAL_MP_PHOTO.format(
            last=clean(last), first=clean(first),
            party=party_abbr.get(caucus.lower(), clean(caucus) or "Ind"))
        out[_pnorm(f"{first} {last}")] = url
    log.info("Photos/MPs (ourcommons XML): %d", len(out))
    return out


def _senators(sess) -> dict[str, str]:
    """Best-effort scrape of the current-senators listing for name + photo."""
    soup = BeautifulSoup(polite_get(sess, SENATE_LIST).text, "lxml")
    out: dict[str, str] = {}
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or "").strip()
        if not src or not alt or len(alt.split()) < 2:
            continue
        if not re.search(r"senator|/media/|/sencanada", src, re.I) and "senator" not in alt.lower():
            continue
        name = re.sub(r"(?i)\b(senator|the honourable|photo of)\b", "", alt).strip()
        if not name:
            continue
        url = src if src.startswith("http") else SENATE_BASE + src
        out[_pnorm(name)] = url
    log.info("Photos/Senators: %d", len(out))
    return out


def fetch_all() -> dict[str, str]:
    """Combine all sources into one {normalized_name: url} map. Each source is
    isolated so a failure in one never drops the others."""
    sess = session()
    photos: dict[str, str] = {}
    # MPs: Represent primary, ourcommons XML fallback only if Represent yielded
    # nothing.
    try:
        photos.update(_mps_represent(sess))
    except Exception as exc:  # noqa: BLE001
        log.warning("Photos/MPs Represent failed: %s", exc)
    if not photos:
        try:
            photos.update(_mps_ourcommons(sess))
        except Exception as exc:  # noqa: BLE001
            log.warning("Photos/MPs ourcommons fallback failed: %s", exc)
    # Senators (additive best-effort).
    try:
        photos.update(_senators(sess))
    except Exception as exc:  # noqa: BLE001
        log.warning("Photos/Senators failed: %s", exc)
    log.info("Photos: %d names total", len(photos))
    return photos
