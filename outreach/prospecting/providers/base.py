"""Base provider interface and credit management."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod

from ..models import ProviderResult


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
        from . import budget
        return budget.status(self.name)["used_month"]

    def has_credits_remaining(self) -> bool:
        """True while this month's and today's budgets have room (see budget.py)."""
        from . import budget
        st = budget.status(self.name)
        return not st["paused"] and st["left_month"] > 0 and st["used_today"] < st["daily_cap"]

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
