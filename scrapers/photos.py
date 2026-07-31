"""Roster + headshots for MPs and Senators.

Emits two maps keyed by normalized name:
  * photos  -> headshot URL
  * roster  -> {chamber: "house"|"senate", party: "L"|"C"|"NDP"|"BQ"|"G"|"NA",
                riding: "<constituency or province>"}

Both are fetched server-side (in CI), so the static app never makes a
cross-origin request — it reads the committed JSON. The roster gives the app
authoritative chamber (to split MPs / Senators / Public Office Holders) and
party (for the avatar ring), replacing hand-maintained overrides. Every source
is best-effort and fail-soft: a failing source contributes nothing and the run
keeps whatever the others (and the previous files) had.

Name matching mirrors the app's `_pnorm`: deburr accents, lowercase, collapse
to single-spaced alphanumerics ("Fayçal El-Khoury" -> "faycal el khoury").
"""
from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .common import log, polite_get, session

REPRESENT = "https://represent.opennorth.ca/representatives/house-of-commons/"
OURCOMMONS_XML = "https://www.ourcommons.ca/members/en/search?output=XML"
OFFICIAL_MP_PHOTO = ("https://www.ourcommons.ca/Content/Parliamentarians/Images/"
                     "OfficialMPPhotos/45/{last}{first}_{party}.jpg")
SENATE_LIST = "https://sencanada.ca/en/senators/"
SENATE_BASE = "https://sencanada.ca"

_HONORIFIC = re.compile(
    r"^\s*(the\s+)?(right\s+|rt\s+)?hon(ourable|orable|\.)?\s+|^\s*(mr|mrs|ms|dr|sen|senator)\.?\s+",
    re.I)

_PARTY_CODE = {
    "liberal": "L", "liberal party of canada": "L",
    "conservative": "C", "conservative party of canada": "C",
    "ndp": "NDP", "new democratic party": "NDP", "new democratic party of canada": "NDP",
    "bloc québécois": "BQ", "bloc quebecois": "BQ",
    "green": "G", "green party": "G", "green party of canada": "G",
    "independent": "NA", "non-affiliated": "NA", "": "NA",
}


def _pnorm(name: str) -> str:
    s = _HONORIFIC.sub("", name or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _party(name: str) -> str:
    return _PARTY_CODE.get((name or "").strip().lower(), "NA")


def _mps_represent(sess, photos: dict, roster: dict) -> int:
    n, offset, limit = 0, 0, 100
    for _ in range(20):  # safety cap
        data = polite_get(sess, REPRESENT,
                          params={"limit": limit, "offset": offset}).json()
        objects = data.get("objects", [])
        for o in objects:
            name = o.get("name")
            if not name:
                continue
            key = _pnorm(name)
            if o.get("photo_url"):
                photos[key] = o["photo_url"]
            roster[key] = {"chamber": "house", "party": _party(o.get("party_name")),
                           "riding": (o.get("district_name") or "").strip()}
            n += 1
        if len(objects) < limit:
            break
        offset += limit
    log.info("Roster/MPs (Represent): %d", n)
    return n


def _mps_ourcommons(sess, photos: dict, roster: dict) -> int:
    """Fallback when Represent is unavailable: names + a constructed
    OfficialMPPhotos URL + caucus/riding from the members XML."""
    root = ET.fromstring(polite_get(sess, OURCOMMONS_XML).content)
    party_abbr = {"L": "Lib", "C": "CPC", "NDP": "NDP", "BQ": "BQ", "G": "GP", "NA": "Ind"}
    n = 0
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
        if not (first and last):
            continue
        caucus = g("CaucusShortName")
        riding = g("ConstituencyName")
        party = _party(caucus)
        key = _pnorm(f"{first} {last}")
        clean = lambda s: re.sub(r"[^A-Za-z]", "", unicodedata.normalize("NFKD", s)
                                 .encode("ascii", "ignore").decode())
        photos[key] = OFFICIAL_MP_PHOTO.format(
            last=clean(last), first=clean(first), party=party_abbr.get(party, "Ind"))
        roster[key] = {"chamber": "house", "party": party, "riding": riding}
        n += 1
    log.info("Roster/MPs (ourcommons XML): %d", n)
    return n


def _senators(sess, photos: dict, roster: dict) -> int:
    """Best-effort scrape of the current-senators listing for name + photo."""
    soup = BeautifulSoup(polite_get(sess, SENATE_LIST).text, "lxml")
    n = 0
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or "").strip()
        if not src or len(alt.split()) < 2:
            continue
        if not re.search(r"senator|/media/|/sencanada", src, re.I) and "senator" not in alt.lower():
            continue
        name = re.sub(r"(?i)\b(senator|the honourable|photo of)\b", "", alt).strip()
        if not name:
            continue
        key = _pnorm(name)
        url = src if src.startswith("http") else SENATE_BASE + src
        photos.setdefault(key, url)
        roster[key] = {"chamber": "senate", "party": "NA", "riding": ""}
        n += 1
    log.info("Roster/Senators: %d", n)
    return n


def fetch() -> tuple[dict, dict]:
    """Return (photos, roster). Each source is isolated so a failure in one
    never drops the others."""
    sess = session()
    photos: dict = {}
    roster: dict = {}
    mps = 0
    try:
        mps = _mps_represent(sess, photos, roster)
    except Exception as exc:  # noqa: BLE001
        log.warning("Roster/MPs Represent failed: %s", exc)
    if not mps:
        try:
            _mps_ourcommons(sess, photos, roster)
        except Exception as exc:  # noqa: BLE001
            log.warning("Roster/MPs ourcommons fallback failed: %s", exc)
    try:
        _senators(sess, photos, roster)
    except Exception as exc:  # noqa: BLE001
        log.warning("Roster/Senators failed: %s", exc)
    log.info("Roster: %d people, %d photos", len(roster), len(photos))
    return photos, roster


def fetch_all() -> dict:
    """Back-compat: just the photos map."""
    return fetch()[0]
