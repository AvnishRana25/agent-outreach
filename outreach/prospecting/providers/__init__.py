"""Provider adapters and fallback dispatcher."""
from __future__ import annotations

from .base import BaseProvider
from .prospeo import ProspeoProvider
from .hunter import HunterProvider
from .skrapp import SkrappProvider
from ..models import ProviderResult


def get_providers() -> list[BaseProvider]:
    """Fallback order: most free credits first, scarcest (Hunter) last."""
    return [ProspeoProvider(), SkrappProvider(), HunterProvider()]


def query_fallback_providers(
    first_name: str,
    last_name: str,
    company: str,
    domain: str,
    prospect_id: int | None = None,
    max_providers: int = 2,
) -> ProviderResult | None:
    """Ask providers in order until one finds the email, spending at most `max_providers` calls on this
    person. Every call goes through the budget (memory, pauses, monthly/daily/per-run caps)."""
    from . import budget
    spent = 0
    for prov in get_providers():
        if spent >= max_providers:
            break
        res = budget.lookup(prov, first_name, last_name, company, domain, prospect_id)
        if res.status in ("verified", "inferred", "not_found") or res.status.startswith(("http_", "error_")):
            spent += 1   # a real call was made
        if res.email:
            return res
    return None


__all__ = [
    "BaseProvider",
    "ProspeoProvider",
    "HunterProvider",
    "SkrappProvider",
    "get_providers",
    "query_fallback_providers"
]
