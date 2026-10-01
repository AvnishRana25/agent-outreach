"""Hunter.io fallback email finder adapter."""
from __future__ import annotations

import requests
from .base import BaseProvider
from ..models import ProviderResult


class HunterProvider(BaseProvider):
    name = "hunter"
    api_key_env = "HUNTER_API_KEY"

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

        url = "https://api.hunter.io/v2/email-finder"
        params = {
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "api_key": self.get_api_key()
        }

        try:
            resp = requests.get(url, params=params, timeout=12.0)
            if resp.status_code == 200:
                data = resp.json()
                data_body = data.get("data") or {}
                email = data_body.get("email")
                score = int(data_body.get("score") or 70)
                verification = data_body.get("verification") or {}
                v_status = verification.get("status")

                if email:
                    self.record_usage(credits_used=1, result=f"found:{email}", prospect_id=prospect_id)
                    status_str = "verified" if v_status == "valid" or score >= 85 else "inferred"
                    return ProviderResult(
                        provider=self.name,
                        email=email.lower().strip(),
                        confidence=score,
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
