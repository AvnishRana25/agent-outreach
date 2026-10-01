"""Provider adapters and fallback dispatcher."""
from __future__ import annotations

from .base import BaseProvider
from .prospeo import ProspeoProvider
from .hunter import HunterProvider
from .skrapp import SkrappProvider
from ..models import ProviderResult


def get_providers() -> list[BaseProvider]:
    """Return available fallback providers in priority sequence."""
    return [
        ProspeoProvider(),
        HunterProvider(),
        SkrappProvider()
    ]


def query_fallback_providers(
    first_name: str,
    last_name: str,
    company: str,
    domain: str,
    prospect_id: int | None = None
) -> ProviderResult | None:
    """Query fallback providers sequentially when local confidence is insufficient."""
    providers = get_providers()
    for prov in providers:
        if prov.is_available() and prov.has_credits_remaining():
            res = prov.find_email(first_name, last_name, company, domain, prospect_id=prospect_id)
            if res and res.email:
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
