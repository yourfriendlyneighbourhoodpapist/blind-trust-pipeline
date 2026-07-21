"""Shared plumbing: polite HTTP session, retries, JSON state storage."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

log = logging.getLogger("blindtrust")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

USER_AGENT = (
    "BlindTrustBot/1.0 (+public-registry tracker; polite crawler; "
    "contact via repository issues)"
)
REQUEST_DELAY = 1.2  # seconds between requests — stay polite


def session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-CA,en;q=0.9"})
    return s


_last_request = 0.0


def polite_get(sess: requests.Session, url: str, **kw) -> requests.Response:
    """GET with a global inter-request delay."""
    global _last_request
    wait = REQUEST_DELAY - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    resp = sess.get(url, timeout=45, **kw)
    _last_request = time.time()
    resp.raise_for_status()
    return resp


def load_json(name: str, default):
    path = DATA_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Corrupt %s — starting fresh", name)
    return default


def save_json(name: str, obj) -> None:
    path = DATA_DIR / name
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    log.info("Wrote %s (%d bytes)", name, path.stat().st_size)
