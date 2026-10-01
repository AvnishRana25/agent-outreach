"""Discover publicly exposed company emails by scraping the official company site."""
from __future__ import annotations

import re
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

from ..validation.syntax import is_valid_syntax, is_role_email
from ... import firecrawl

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

COMMON_PATHS = [
    "/", "/about", "/about-us", "/team", "/our-team", "/people",
    "/leadership", "/contact", "/contact-us", "/careers", "/press"
]

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _fetch_html(url: str, timeout: tuple[float, float] = (2.5, 4.0)) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except Exception:
        pass
    return ""


def discover_emails_from_site(domain: str, max_pages: int = 6) -> dict[str, Any]:
    """Scrape company site to discover known employee and role email addresses.
    
    Returns:
        {
            "employee_emails": list[str],
            "role_emails": list[str],
            "all_emails": list[str],
            "sources": dict[str, str], # email -> url found on
            "pages_visited": list[str]
        }
    """
    if not domain:
        return {"employee_emails": [], "role_emails": [], "all_emails": [], "sources": {}, "pages_visited": []}

    base = f"https://{domain}"
    dom_clean = domain.strip().lower().removeprefix("www.")

    employee_emails: set[str] = set()
    role_emails: set[str] = set()
    sources: dict[str, str] = {}
    visited: list[str] = []

    for path in COMMON_PATHS[:max_pages]:
        page_url = urljoin(base, path)
        html = _fetch_html(page_url)
        visited.append(page_url)

        if not html and path == "/":
            # If plain fetch fails on root, try Firecrawl fallback if available
            try:
                fc = firecrawl.scrape(page_url)
                if fc and (fc.get("html") or fc.get("markdown")):
                    html = fc.get("html") or fc.get("markdown") or ""
            except Exception:
                pass

        if not html:
            continue

        # Extract emails from HTML and text
        raw_matches = EMAIL_REGEX.findall(html)
        for raw in raw_matches:
            email = raw.strip().lower()
            if not is_valid_syntax(email):
                continue
            e_dom = email.split("@")[1].removeprefix("www.")
            if e_dom != dom_clean:
                continue

            if email not in sources:
                sources[email] = page_url

            if is_role_email(email):
                role_emails.add(email)
            else:
                employee_emails.add(email)

        # Early exit if we already found 3+ verified employee emails
        if len(employee_emails) >= 4:
            break

    return {
        "employee_emails": sorted(employee_emails),
        "role_emails": sorted(role_emails),
        "all_emails": sorted(employee_emails | role_emails),
        "sources": sources,
        "pages_visited": visited
    }
