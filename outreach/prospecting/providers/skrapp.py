"""Skrapp.io fallback email finder adapter."""
from __future__ import annotations

import requests
from .base import BaseProvider
from ..models import ProviderResult


class SkrappProvider(BaseProvider):
    name = "skrapp"
    api_key_env = "SKRAPP_API_KEY"

    def find_email(
        self,
        first_name: str,
        last_name: str,
        company: str,
        domain: str,
        prospect_id: int | None = None
    ) -> ProviderResult:
        if not self.is_available():
            return ProviderResult(provider=self.name, status="unconfigured")

        if not self.has_credits_remaining():
            return ProviderResult(provider=self.name, status="limit_reached")

        url = "https://api.skrapp.io/api/v2/find"
        headers = {
            "X-Access-Key": self.get_api_key(),
            "Content-Type": "application/json",
            "User-Agent": "agent-outreach-prospecting/1.0"
        }
        params = {
            "firstName": first_name,
            "lastName": last_name,
            "domain": domain,
            "company": company
        }

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=12.0)
            if resp.status_code == 200:
                data = resp.json()
                email = data.get("email")
                accuracy = int(data.get("accuracy") or 70)

                if email:
                    self.record_usage(credits_used=1, result=f"found:{email}", prospect_id=prospect_id)
                    status_str = "verified" if accuracy >= 85 else "inferred"
                    return ProviderResult(
                        provider=self.name,
                        email=email.lower().strip(),
                        confidence=accuracy,
                        status=status_str,
                        credits_used=1,
                        raw=data
                    )
                else:
                    self.record_usage(credits_used=0, result="not_found", prospect_id=prospect_id)
                    return ProviderResult(provider=self.name, status="not_found", raw=data)
            else:
                return ProviderResult(provider=self.name, status=f"http_{resp.status_code}")
        except Exception as e:
            return ProviderResult(provider=self.name, status=f"error_{type(e).__name__}")
