"""Resolve official company domain from company name."""
from __future__ import annotations

import re
import urllib.parse
import requests
from typing import Callable

from .normalizer import normalize_domain, is_ignored_domain
from ... import db, firecrawl

CLEAN_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|llp|ltd|limited|corp|corporation|pvt|private|gmbh|co|company|technologies|tech|solutions|group|holdings)\b",
    re.I
)

# In-memory session cache for domain resolution
_DOMAIN_RESOLVE_CACHE: dict[str, str] = {}


def clean_company_name(name: str) -> str:
    """Normalize company name by stripping legal suffixes and punctuation."""
    if not name:
        return ""
    cleaned = re.sub(r"[,.\-_\(\)\/]+", " ", name)
    cleaned = CLEAN_SUFFIXES.sub("", cleaned)
    return " ".join(cleaned.split()).strip()


def resolve_domain_from_db(company: str) -> str:
    """Check if the local database already has a recorded domain for this company."""
    if not company:
        return ""
    try:
        with db.connect() as conn:
            # Check prospect_domain_cache
            row = conn.execute(
                "SELECT domain FROM prospect_domain_cache WHERE lower(domain) LIKE ?",
                (f"%{company.lower().replace(' ', '')}%",)
            ).fetchone()
            if row and row[0]:
                return row[0]
            # Check leads table
            row = conn.execute(
                "SELECT domain FROM leads WHERE lower(company) = ? AND domain != '' LIMIT 1",
                (company.lower().strip(),)
            ).fetchone()
            if row and row[0]:
                return normalize_domain(row[0])
    except Exception:
        pass
    return ""


def resolve_domain_from_search(company: str) -> str:
    """Search public sources or Firecrawl to locate official company website."""
    if not company:
        return ""

    query = f"{company} official website"

    # 1. Firecrawl search if configured
    try:
        fc_results = firecrawl.search(query)
        if fc_results:
            for item in fc_results:
                url = item.get("url") or ""
                dom = normalize_domain(url)
                if dom and not is_ignored_domain(dom):
                    return dom
    except Exception:
        pass

    # 2. Free public search (DuckDuckGo HTML)
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if resp.ok:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a.result__url, a.result__snippet, .result__body a"):
                href = a.get("href") or ""
                # DDG links wrap target in uddg= param
                if "uddg=" in href:
                    m = re.search(r"uddg=([^&]+)", href)
                    if m:
                        href = urllib.parse.unquote(m.group(1))
                dom = normalize_domain(href)
                if dom and not is_ignored_domain(dom):
                    return dom
    except Exception:
        pass

    return ""


def resolve_domain(company: str, raw_domain: str | None = None) -> str:
    """Determine official company domain from raw input or company name.
    
    1. If raw_domain is valid, normalize and return it.
    2. Otherwise, check cache, DB, and public search.
    """
    if raw_domain:
        norm = normalize_domain(raw_domain)
        if norm:
            return norm

    if not company:
        return ""

    key = company.lower().strip()
    if key in _DOMAIN_RESOLVE_CACHE:
        return _DOMAIN_RESOLVE_CACHE[key]

    # Check local database
    db_dom = resolve_domain_from_db(company)
    if db_dom:
        _DOMAIN_RESOLVE_CACHE[key] = db_dom
        return db_dom

    # Check web search / Firecrawl
    search_dom = resolve_domain_from_search(company)
    if search_dom:
        _DOMAIN_RESOLVE_CACHE[key] = search_dom
        return search_dom

    # Fallback heuristic: clean name as .com
    slug = re.sub(r"[^a-z0-9]", "", clean_company_name(company).lower())
    if slug:
        candidate = f"{slug}.com"
        _DOMAIN_RESOLVE_CACHE[key] = candidate
        return candidate

    return ""
