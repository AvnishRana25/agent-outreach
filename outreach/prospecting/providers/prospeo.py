"""Prospeo email finder (free plan). Called only through budget.lookup(), which counts and limits calls."""
from __future__ import annotations

import requests

from ..models import ProviderResult
from .base import BaseProvider


class ProspeoProvider(BaseProvider):
    name = "prospeo"
    api_key_env = "PROSPEO_API_KEY"

    def find_email(self, first_name: str, last_name: str, company: str, domain: str,
                   prospect_id: int | None = None) -> ProviderResult:
        resp = requests.post("https://api.prospeo.io/email-finder", timeout=15,
                             headers={"X-KEY": self.get_api_key(), "Content-Type": "application/json",
                                      "User-Agent": "agent-outreach-prospecting/1.0"},
                             json={"first_name": first_name, "last_name": last_name, "company": domain or company})
        if resp.status_code != 200:
            # Prospeo answers 400 with NO_RESULT for "not found" (nothing charged); keep the code otherwise
            try:
                err = str((resp.json() or {}).get("message") or (resp.json() or {}).get("error_code") or "")
            except ValueError:
                err = ""
            if resp.status_code == 400 and "NO_RESULT" in err.upper().replace(" ", "_"):
                return ProviderResult(provider=self.name, status="not_found")
            return ProviderResult(provider=self.name, status=f"http_{resp.status_code}")
        data = resp.json()
        body = data.get("response") or {}
        found = body.get("email")
        if isinstance(found, dict):  # {"email": "...", "email_status"/"verification": "VALID", ...}
            email = found.get("email")
            verdict = str(found.get("email_status") or found.get("verification") or found.get("status") or "").upper()
        else:
            email, verdict = found, str(body.get("email_status") or "").upper()
        if data.get("error") or not email:
            return ProviderResult(provider=self.name, status="not_found", raw=data)
        verified = verdict in ("VALID", "VERIFIED")
        score = int(body.get("score") or (92 if verified else 70))
        return ProviderResult(provider=self.name, email=str(email).lower().strip(), confidence=score,
                              status="verified" if verified or score >= 85 else "inferred", credits_used=1, raw=data)
