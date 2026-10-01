"""Prospeo fallback email finder adapter."""
from __future__ import annotations

import requests
from .base import BaseProvider
from ..models import ProviderResult


class ProspeoProvider(BaseProvider):
    name = "prospeo"
    api_key_env = "PROSPEO_API_KEY"

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

        url = "https://api.prospeo.io/email-finder"
        headers = {
            "X-KEY": self.get_api_key(),
            "Content-Type": "application/json",
            "User-Agent": "agent-outreach-prospecting/1.0"
        }
        payload = {
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "domain": domain
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=12.0)
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("error"):
                    res_body = data.get("response") or {}
                    email = res_body.get("email")
                    email_status = res_body.get("email_status", "").upper()
                    score = int(res_body.get("score") or (92 if email_status == "VERIFIED" else 70))
                    
                    if email:
                        self.record_usage(credits_used=1, result=f"found:{email}", prospect_id=prospect_id)
                        status_str = "verified" if score >= 85 else "inferred"
                        return ProviderResult(
                            provider=self.name,
                            email=email.lower().strip(),
                            confidence=score,
                            status=status_str,
                            credits_used=1,
                            raw=data
                        )
                # No email found or error
                self.record_usage(credits_used=0, result="not_found", prospect_id=prospect_id)
                return ProviderResult(provider=self.name, status="not_found", raw=data)
            else:
                return ProviderResult(provider=self.name, status=f"http_{resp.status_code}")
        except Exception as e:
            return ProviderResult(provider=self.name, status=f"error_{type(e).__name__}")
