"""Find a company's own website when a source only gives its name (job boards, registries).

Order: a link in the source text -> a guessed domain that is checked by visiting it.
A guess only counts if the page actually names the company (or shows its registration number),
so a parked domain or a namesake never becomes a lead.
"""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126 Safari/537.36"}
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

# Links in job posts that are never the company's own site.
NOT_COMPANY = ("remotive.com", "himalayas.app", "remoteok.com", "jobicy.com", "weworkremotely.com",
               "lever.co", "greenhouse.io", "ashbyhq.com", "workable.com", "bamboohr.com", "recruitee.com",
               "breezy.hr", "smartrecruiters.com", "wellfound.com", "angel.co", "linkedin.com", "indeed.",
               "glassdoor.", "ycombinator.com", "github.com", "google.com", "goo.gl", "bit.ly", "forms.gle",
               "notion.site", "notion.so", "typeform.com", "calendly.com", "twitter.com", "x.com",
               "facebook.com", "instagram.com", "youtube.com", "medium.com", "reddit.com", "t.co",
               "wa.me", "whatsapp.com", "apply.workable", "jobs.", "careers.")

# Words dropped from a company name before guessing its domain.
SUFFIXES = r"\b(ltd|limited|llc|inc|incorporated|plc|llp|pvt|private|co|corp|corporation|gmbh|" \
           r"fz|fze|fzco|fz-llc|dmcc|real estate brokers?|properties|property|brokers?|group|holdings?|the|and|&)\b"
STOP = {"ltd", "limited", "llc", "inc", "the", "and", "group", "co", "company", "agency", "uk", "digital"}


def from_text(text: str) -> str:
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,;")
        host = urlparse(url).netloc.lower()
        if host and not any(bad in host for bad in NOT_COMPANY):
            return f"https://{host}"
    return ""


def slugs(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 &-]", " ", (name or "").lower().replace(".", ""))
    base = re.sub(SUFFIXES, " ", base)
    words = [w for w in re.split(r"[\s&-]+", base) if w]
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    if len(words) > 2:
        out.append("".join(words[:2]))
    return list(dict.fromkeys(s for s in out if 2 < len(s) <= 40))


def name_tokens(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(w) > 2 and w not in STOP]


@lru_cache(maxsize=4096)
def _resolves(host: str) -> bool:
    try:
        return bool(dns.resolver.resolve(host, "A", lifetime=5))
    except (dns.exception.DNSException, ValueError):
        return False


def _fetch(url: str) -> str:
    try:
        r = requests.get(url, headers=UA, timeout=10, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", ""):
            return r.text[:300_000]
    except requests.RequestException:
        pass
    return ""


def page_matches(html: str, name: str, must_contain: str = "") -> bool:
    if not html:
        return False
    low = html.lower()
    if must_contain:
        return must_contain.lower() in low
    toks = name_tokens(name)
    if not toks:
        return False
    hits = sum(t in low for t in toks)
    return hits >= max(1, round(len(toks) * 0.6))


def guess(name: str, tlds: list[str], must_contain: str = "") -> str:
    """Try name-based domains on the given TLDs; return the first one whose page names the company."""
    for slug in slugs(name):
        for tld in tlds:
            host = f"{slug}.{tld.lstrip('.')}"
            if not _resolves(host):
                continue
            url = f"https://{host}"
            if page_matches(_fetch(url), name, must_contain):
                return url
    return ""


def search_site(name: str, hint: str = "", must_contain: str = "") -> str:
    """Firecrawl web search for the company's site; only a page that names the company is accepted."""
    from . import firecrawl
    for hit in firecrawl.search(f"{name} {hint}".strip(), limit=5):
        host = urlparse(hit["url"]).netloc.lower()
        if not host or any(bad in host for bad in NOT_COMPANY + ("wikipedia.org", "bayut.com",
                                                                  "propertyfinder", "dubizzle", "yelp.",
                                                                  "companieshouse", "find-and-update")):
            continue
        url = f"https://{host}"
        if page_matches(_fetch(url), name, must_contain):
            return url
    return ""


def resolve(name: str, tlds: list[str], must_contain: str = "", hint: str = "") -> str:
    """Guess the domain for free first; spend a Firecrawl search only if that fails."""
    return guess(name, tlds, must_contain) or search_site(name, hint, must_contain)


def find(name: str, text: str = "", tlds: list[str] | None = None, must_contain: str = "", hint: str = "") -> str:
    return from_text(text) or resolve(name, tlds or ["com", "io", "ai", "co"], must_contain, hint)
