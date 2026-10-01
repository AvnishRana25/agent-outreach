"""Base provider interface and credit management."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from abc import ABC, abstractmethod

from ..models import ProviderResult
from ..config import provider_monthly_limit
from ... import db


class BaseProvider(ABC):
    """Abstract base class for external fallback email providers."""
    name: str = "base"
    api_key_env: str = ""

    def is_available(self) -> bool:
        """Check if provider API key is configured."""
        if not self.api_key_env:
            return False
        return bool(os.getenv(self.api_key_env, "").strip())

    def get_api_key(self) -> str:
        return os.getenv(self.api_key_env, "").strip()

    def credits_used_this_month(self) -> int:
        """Query database for credits consumed by this provider in the current calendar month."""
        start_of_month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        try:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(credits_used), 0) FROM provider_credits "
                    "WHERE provider=? AND request_timestamp >= ?",
                    (self.name, start_of_month)
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def has_credits_remaining(self) -> bool:
        """Check if usage is below configured monthly limit."""
        limit = provider_monthly_limit(self.name)
        used = self.credits_used_this_month()
        return used < limit

    def record_usage(self, credits_used: int = 1, result: str = "", prospect_id: int | None = None) -> None:
        """Persist credit consumption event in database."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO provider_credits (provider, credits_used, request_timestamp, result, prospect_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.name, credits_used, now_iso, result, prospect_id)
                )
        except Exception:
            pass

    @abstractmethod
    def find_email(
        self,
        first_name: str,
        last_name: str,
        company: str,
        domain: str,
        prospect_id: int | None = None
    ) -> ProviderResult:
        """Query provider to find business email for prospect."""
        pass
