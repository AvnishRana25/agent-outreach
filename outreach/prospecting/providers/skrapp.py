"""Skrapp.io email finder (free plan). Called only through budget.lookup(), which counts and limits calls."""
from __future__ import annotations

import requests

from ..models import ProviderResult
from .base import BaseProvider


class SkrappProvider(BaseProvider):
    name = "skrapp"
    api_key_env = "SKRAPP_API_KEY"

    def find_email(self, first_name: str, last_name: str, company: str, domain: str,
                   prospect_id: int | None = None) -> ProviderResult:
        resp = requests.get("https://api.skrapp.io/api/v2/find", timeout=15,
                            params={"firstName": first_name, "lastName": last_name, "domain": domain, "company": company},
                            headers={"X-Access-Key": self.get_api_key(), "Content-Type": "application/json",
                                     "User-Agent": "agent-outreach-prospecting/1.0"})
        if resp.status_code != 200:
            return ProviderResult(provider=self.name, status=f"http_{resp.status_code}")
        data = resp.json()
        email = data.get("email")
        if not email:
            return ProviderResult(provider=self.name, status="not_found", raw=data)
        accuracy = int(data.get("accuracy") or 70)
        return ProviderResult(provider=self.name, email=email.lower().strip(), confidence=accuracy,
                              status="verified" if accuracy >= 85 else "inferred", credits_used=1, raw=data)
