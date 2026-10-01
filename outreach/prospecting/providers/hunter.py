"""Hunter.io email finder (free plan). Called only through budget.lookup(), which counts and limits calls.
The key goes in a header, not the URL, so it never lands in logs or error messages."""
from __future__ import annotations

import requests

from ..models import ProviderResult
from .base import BaseProvider


class HunterProvider(BaseProvider):
    name = "hunter"
    api_key_env = "HUNTER_API_KEY"

    def find_email(self, first_name: str, last_name: str, company: str, domain: str,
                   prospect_id: int | None = None) -> ProviderResult:
        params = {"first_name": first_name, "last_name": last_name}
        params.update({"domain": domain} if domain else {"company": company})
        resp = requests.get("https://api.hunter.io/v2/email-finder", params=params, timeout=15,
                            headers={"X-API-KEY": self.get_api_key()})
        if resp.status_code != 200:
            return ProviderResult(provider=self.name, status=f"http_{resp.status_code}")
        data = resp.json()
        body = data.get("data") or {}
        email = body.get("email")
        if not email:
            return ProviderResult(provider=self.name, status="not_found", raw=data)
        score = int(body.get("score") or 70)
        verified = (body.get("verification") or {}).get("status") == "valid"
        return ProviderResult(provider=self.name, email=email.lower().strip(), confidence=score,
                              status="verified" if verified or score >= 85 else "inferred", credits_used=1, raw=data)
