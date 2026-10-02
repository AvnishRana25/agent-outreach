"""Prospeo verified person email lookup. Calls go through budget.lookup()."""

from __future__ import annotations

import requests

from ..models import ProviderResult
from .base import BaseProvider


class ProspeoProvider(BaseProvider):
    name = "prospeo"
    api_key_env = "PROSPEO_API_KEY"

    def find_email(self, first_name: str, last_name: str, company: str, domain: str,
                   prospect_id: int | None = None) -> ProviderResult:
        resp = requests.post("https://api.prospeo.io/enrich-person", timeout=15,
                             headers={"X-KEY": self.get_api_key(), "Content-Type": "application/json",
                                      "User-Agent": "agent-outreach-prospecting/1.0"},
                             json={"only_verified_email": True, "data": {
                                 "first_name": first_name, "last_name": last_name,
                                 "company_website": domain, "company_name": company}})
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 400 and data.get("error_code") == "NO_MATCH":
            return ProviderResult(provider=self.name, status="not_found")
        if resp.status_code != 200 or data.get("error"):
            return ProviderResult(provider=self.name, status=f"http_{resp.status_code}")
        found = ((data.get("person") or {}).get("email") or {})
        email = found.get("email") or ""
        if found.get("status") != "VERIFIED" or not email or "*" in email or not found.get("revealed"):
            return ProviderResult(provider=self.name, status="not_found")
        return ProviderResult(provider=self.name, email=email.lower().strip(), confidence=92,
                              status="verified", credits_used=0 if data.get("free_enrichment") else 1, raw=data)
