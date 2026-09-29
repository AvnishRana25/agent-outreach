"""Firecrawl (firecrawl.dev) as a fallback for the pages a plain download can't read.

Used in three places, only when the cheap path fails, to keep credit use low:
  scrape  enrich: a site that comes back empty or as a JavaScript shell (Wix, Webflow, React)
          is loaded in a real browser, so there is real text to personalise on.
  map     enrich: lists a site's real pages, so contact/about/team/careers pages are found even
          when they aren't at /contact, /about... (more published emails, more founder names).
  search  website finder: a company known only by name (job boards, registers) whose domain
          can't be guessed is looked up; the result is still checked before it's accepted.

Every call is counted against `firecrawl.daily_credit_cap` in settings.yaml. At the cap the
engine silently falls back to the free path, so an empty balance never stops a run.
Works with the hosted API (FIRECRAWL_API_KEY) or a self-hosted copy (FIRECRAWL_API_URL).
"""
from __future__ import annotations

import os
from datetime import date

import requests

from . import config, db

DEFAULT_URL = "https://api.firecrawl.dev"


def _cfg() -> dict:
    return config.settings().get("firecrawl", {}) or {}


def _base() -> str:
    return os.getenv("FIRECRAWL_API_URL", DEFAULT_URL).rstrip("/")


def enabled(feature: str) -> bool:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return False
    if _base() == DEFAULT_URL and not os.getenv("FIRECRAWL_API_KEY"):
        return False
    return feature in cfg.get("use_for", ["scrape", "map", "search"])


def _spend(credits: int) -> bool:
    """Reserve credits for today; False when the daily cap would be exceeded."""
    cap = int(_cfg().get("daily_credit_cap", 60))
    key = f"firecrawl:{date.today().isoformat()}"
    with db.connect() as conn:
        used = int(db.get_state(conn, key, "0"))
        if used + credits > cap:
            return False
        db.set_state(conn, key, used + credits)
    return True


def used_today() -> int:
    with db.connect() as conn:
        return int(db.get_state(conn, f"firecrawl:{date.today().isoformat()}", "0"))


def _post(path: str, body: dict, credits: int, timeout: int = 90) -> dict | None:
    if not _spend(credits):
        return None
    headers = {"Content-Type": "application/json"}
    if os.getenv("FIRECRAWL_API_KEY"):
        headers["Authorization"] = f"Bearer {os.getenv('FIRECRAWL_API_KEY')}"
    try:
        r = requests.post(f"{_base()}/v2/{path}", json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        print(f"    firecrawl {path}: {e.__class__.__name__}")
        return None
    if r.status_code == 402:
        print("    firecrawl: out of credits; continuing without it")
        with db.connect() as conn:  # stop trying for the rest of the day
            db.set_state(conn, f"firecrawl:{date.today().isoformat()}", 10**6)
        return None
    if not r.ok:
        print(f"    firecrawl {path}: HTTP {r.status_code} {r.text[:150]}")
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if data.get("success", True) else None


def scrape(url: str) -> dict | None:
    """-> {'html': raw html, 'markdown': page text, 'links': [...], 'title': str} or None."""
    if not enabled("scrape"):
        return None
    data = _post("scrape", {"url": url, "formats": ["markdown", "rawHtml", "links"],
                            "onlyMainContent": False, "timeout": 45000}, credits=1)
    doc = (data or {}).get("data") or {}
    if not doc:
        return None
    meta = doc.get("metadata") or {}
    return {"html": doc.get("rawHtml") or doc.get("html") or "", "markdown": doc.get("markdown") or "",
            "links": doc.get("links") or [], "title": meta.get("title") or ""}


def site_map(url: str, limit: int = 100) -> list[str]:
    if not enabled("map"):
        return []
    data = _post("map", {"url": url, "limit": limit}, credits=1)
    links = (data or {}).get("links") or []
    return [l["url"] if isinstance(l, dict) else l for l in links if l]


def search(query: str, limit: int = 5) -> list[dict]:
    """-> [{'url', 'title', 'description'}]. Counted as 2 credits: a conservative estimate."""
    if not enabled("search"):
        return []
    data = _post("search", {"query": query, "limit": limit}, credits=2)
    results = (data or {}).get("data") or []
    if isinstance(results, dict):  # v2 groups results by type
        results = results.get("web") or []
    return [{"url": x.get("url", ""), "title": x.get("title", ""), "description": x.get("description", "")}
            for x in results if isinstance(x, dict) and x.get("url")]
