"""Domain normalization and filtering."""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Obvious social media, portfolio, directory, and repository domains to ignore as official company websites
IGNORED_DOMAINS = {
    "linkedin.com", "www.linkedin.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "facebook.com", "www.facebook.com", "fb.com",
    "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com",
    "github.com", "www.github.com",
    "crunchbase.com", "www.crunchbase.com",
    "bloomberg.com", "www.bloomberg.com",
    "pitchbook.com", "www.pitchbook.com",
    "yelp.com", "www.yelp.com",
    "glassdoor.com", "www.glassdoor.com",
    "wikipedia.org", "www.wikipedia.org",
    "zoominfo.com", "www.zoominfo.com",
    "apollo.io", "www.apollo.io",
    "medium.com", "www.medium.com",
    "substack.com", "www.substack.com",
    "linktr.ee", "www.linktr.ee",
    "threads.net", "www.threads.net",
    "tiktok.com", "www.tiktok.com",
    "wellfound.com", "angel.co",
    "google.com", "maps.google.com"
}

# Regex to strip subdomains that are not part of the apex/main brand (e.g. m., mobile.)
SUBDOMAIN_STRIP = re.compile(r"^(?:m|mobile|blog|news|support|help|docs|api|cdn)\.", re.I)


def is_ignored_domain(domain: str) -> bool:
    """Check if domain is a known social network, directory, or public aggregator."""
    if not domain:
        return True
    dom = domain.lower().strip()
    if dom in IGNORED_DOMAINS:
        return True
    for ig in IGNORED_DOMAINS:
        if dom.endswith("." + ig):
            return True
    return False


def normalize_domain(raw: str | None) -> str:
    """Normalize URLs or domain strings to a clean canonical apex domain.
    
    Examples:
        'https://www.acme.com/about' -> 'acme.com'
        'www.acme.com' -> 'acme.com'
        'acme.com/' -> 'acme.com'
        'https://linkedin.com/company/acme' -> '' (ignored domain)
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # Prepend scheme if missing so urlparse extracts netloc correctly
    if "://" not in text:
        text = "https://" + text

    try:
        parsed = urlparse(text)
        host = (parsed.netloc or parsed.path).split("/")[0].split(":")[0].strip().lower()
    except Exception:
        return ""

    # Remove leading www.
    host = host.removeprefix("www.")
    # Remove common subdomains (blog., docs., m., etc.)
    host = SUBDOMAIN_STRIP.sub("", host)

    # Remove trailing dot or slashes
    host = host.rstrip("./")

    # If this is an ignored social or directory domain, treat as empty
    if is_ignored_domain(host):
        return ""

    # Check for basic valid domain structure: at least one dot, no spaces
    if "." not in host or " " in host or len(host) < 4:
        return ""

    # Basic regex validation for domain characters
    if not re.match(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$", host):
        return ""

    return host
