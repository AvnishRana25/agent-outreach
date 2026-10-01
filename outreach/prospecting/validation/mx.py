"""DNS and MX record validation with caching."""
from __future__ import annotations

import dns.resolver
import dns.exception

# In-memory LRU cache for MX lookups
_MX_CACHE: dict[str, tuple[bool, list[str], str]] = {}


def detect_provider(mx_hosts: list[str]) -> str:
    """Infer email hosting provider from MX exchange hostnames."""
    joined = " ".join(mx_hosts).lower()
    if "google.com" in joined or "googlemail.com" in joined:
        return "Google Workspace"
    if "outlook.com" in joined or "microsoft.com" in joined:
        return "Microsoft 365"
    if "zoho." in joined:
        return "Zoho Mail"
    if "protonmail." in joined or "proton.ch" in joined:
        return "Proton Mail"
    if "mimecast." in joined:
        return "Mimecast"
    if "barracuda." in joined:
        return "Barracuda"
    if "secureserver.net" in joined:
        return "GoDaddy"
    if "ovh." in joined:
        return "OVH"
    return "Custom/Self-hosted"


def check_mx(domain: str) -> tuple[bool, list[str], str]:
    """Check if domain has valid MX records.
    
    Returns:
        (has_mx: bool, mx_hosts: list[str], provider: str)
    """
    if not domain:
        return False, [], "Unknown"

    dom = domain.strip().lower().removeprefix("www.")
    if dom in _MX_CACHE:
        return _MX_CACHE[dom]

    hosts: list[str] = []
    try:
        answers = dns.resolver.resolve(dom, "MX", lifetime=6.0)
        for rdata in answers:
            hosts.append(str(rdata.exchange).rstrip(".").lower())
        res = (len(hosts) > 0, hosts, detect_provider(hosts))
        _MX_CACHE[dom] = res
        return res
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        pass
    except dns.exception.Timeout:
        # On DNS timeout, don't fail permanently; assume possible MX
        return True, [], "Timeout (presumed valid)"
    except Exception:
        pass

    # RFC fallback: domain with an A record can theoretically accept mail if no MX
    try:
        a_answers = dns.resolver.resolve(dom, "A", lifetime=4.0)
        if len(a_answers) > 0:
            res = (True, [dom], "A-Record Fallback")
            _MX_CACHE[dom] = res
            return res
    except Exception:
        pass

    res = (False, [], "No MX records")
    _MX_CACHE[dom] = res
    return res
