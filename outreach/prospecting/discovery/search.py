"""Public search discovery for company employee emails and candidate email verification."""
from __future__ import annotations

import re
import urllib.parse
import requests
from bs4 import BeautifulSoup

from ..validation.syntax import is_valid_syntax, is_role_email
from ... import firecrawl

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _ddg_search(query: str, limit: int = 5) -> list[tuple[str, str]]:
    """Execute a light DuckDuckGo HTML search and return list of (title_and_snippet, url)."""
    results: list[tuple[str, str]] = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=(2.5, 4.0))
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select(".result"):
                a = result.select_one(".result__title a")
                snippet = result.select_one(".result__snippet")
                text = (a.get_text() if a else "") + " " + (snippet.get_text() if snippet else "")
                href = a.get("href") if a else ""
                if "uddg=" in href:
                    m = re.search(r"uddg=([^&]+)", href)
                    if m:
                        href = urllib.parse.unquote(m.group(1))
                if text.strip():
                    results.append((text.strip(), href))
                if len(results) >= limit:
                    break
    except Exception:
        pass
    return results


def search_company_emails(domain: str) -> dict[str, str]:
    """Search public search results for any emails on the company's domain.
    
    Returns:
        dict of {email: source_url}
    """
    if not domain:
        return {}
    dom_clean = domain.strip().lower().removeprefix("www.")
    found: dict[str, str] = {}

    queries = [f'site:{dom_clean} "@"', f'"{dom_clean}" "@"']
    for q in queries:
        items = _ddg_search(q, limit=5)
        for text, url in items:
            matches = EMAIL_REGEX.findall(text)
            for m in matches:
                email = m.lower().strip()
                if is_valid_syntax(email) and email.endswith(f"@{dom_clean}"):
                    if email not in found:
                        found[email] = url

    return found


def search_exact_candidate_email(candidate_email: str) -> tuple[bool, str | None]:
    """Check if an exact candidate email appears in public web search results."""
    if not candidate_email or not is_valid_syntax(candidate_email):
        return False, None

    query = f'"{candidate_email}"'

    # Try Firecrawl search if configured
    try:
        fc = firecrawl.search(query)
        if fc:
            for item in fc:
                content = (item.get("content") or "") + " " + (item.get("title") or "")
                if candidate_email.lower() in content.lower():
                    return True, item.get("url")
    except Exception:
        pass

    # Free DDG search
    items = _ddg_search(query, limit=4)
    for text, url in items:
        if candidate_email.lower() in text.lower():
            return True, url

    return False, None


def search_name_with_domain(full_name: str, domain: str) -> tuple[str | None, str | None]:
    """Search for combinations like '"Alex Smith" "@acme.com"' to locate specific employee email."""
    if not full_name or not domain:
        return None, None

    dom_clean = domain.strip().lower().removeprefix("www.")
    query = f'"{full_name}" "@{dom_clean}"'

    items = _ddg_search(query, limit=4)
    for text, url in items:
        matches = EMAIL_REGEX.findall(text)
        for m in matches:
            email = m.lower().strip()
            if is_valid_syntax(email) and email.endswith(f"@{dom_clean}") and not is_role_email(email):
                return email, url

    return None, None
