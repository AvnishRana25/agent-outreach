"""Enriched contact email verification models and quality gate checks.

Provides structured verification status, confidence score, catch-all detection,
and strict evaluation before outreach sends.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field

from . import config, db


class VerificationDetail(BaseModel):
    status: str = Field(default="unchecked", description="valid | risky | invalid | unchecked | guessed | unconfirmed")
    confidence: int = Field(default=0, description="Verification confidence score (0 - 100)")
    is_catch_all: bool = Field(default=False, description="Whether the receiving domain accepts all mailboxes")
    provider: str = Field(default="", description="Verification source/provider (e.g. hunter, prospeo, dns, public_match)")
    method: str = Field(default="", description="Verification method (mx | dns | public_match | pattern | smtp | provider)")
    verified_at: str = Field(default="", description="ISO timestamp when verification occurred")
    raw_details: dict[str, Any] = Field(default_factory=dict, description="Raw details from provider or checks")

    def effective_confidence(self) -> int:
        """Apply catch-all penalty (-25) unless strongly verified via direct SMTP or public match."""
        if self.is_catch_all and self.method not in ("public_match", "smtp"):
            return max(0, self.confidence - 25)
        return self.confidence


def get_lead_verification_detail(lead: dict | Any, conn=None) -> VerificationDetail:
    """Retrieve or construct the VerificationDetail for a given lead."""
    lead = dict(lead) if lead is not None else {}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw = lead.get("email_verification_detail")

    if raw:
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(d, dict) and ("confidence" in d or "confidence_score" in d):
                return VerificationDetail(
                    status=d.get("status", lead.get("email_status", "unchecked")),
                    confidence=int(d.get("confidence", d.get("confidence_score", 0))),
                    is_catch_all=bool(d.get("is_catch_all", False)),
                    provider=d.get("provider", ""),
                    method=d.get("method", ""),
                    verified_at=d.get("verified_at", now_iso),
                    raw_details=d.get("raw_details", {})
                )
        except (json.JSONDecodeError, TypeError):
            pass

    # Infer from existing lead metadata
    email_status = (lead.get("email_status") if isinstance(lead, dict) else getattr(lead, "email_status", None)) or "unchecked"
    email_source = (lead.get("email_source") if isinstance(lead, dict) else getattr(lead, "email_source", None)) or ""
    domain = (lead.get("domain") if isinstance(lead, dict) else getattr(lead, "domain", None)) or ""

    # Check if domain is marked as catch-all in database cache
    is_catch_all = False
    if conn and domain:
        try:
            row = conn.execute("SELECT is_catch_all FROM prospect_domain_cache WHERE domain=?", (domain,)).fetchone()
            if row and row["is_catch_all"]:
                is_catch_all = True
        except Exception:
            pass

    # Determine confidence and method
    confidence = 0
    method = "dns"
    provider = "local"

    if email_source in ("website", "public_match", "post"):
        # Publicly published match on official site or post
        status = "valid"
        confidence = 95
        method = "public_match"
        provider = "public_source"
        is_catch_all = False  # Explicit public match is real regardless of domain catch-all
    elif email_source == "provider_verified":
        status = "valid"
        confidence = 92
        method = "smtp"
        provider = "provider"
    elif email_status == "valid":
        if is_catch_all:
            # Catch-all domain without strong public proof fails the strong confidence bar
            status = "valid"
            confidence = 60
            method = "mx_catch_all"
            provider = "dns"
        else:
            status = "valid"
            confidence = 85
            method = "mx"
            provider = "dns"
    elif email_status == "risky":
        status = "risky"
        confidence = 65
        method = "role"
        provider = "dns"
    elif email_status == "guessed":
        status = "guessed"
        confidence = 40
        method = "pattern"
        provider = "local"
    elif email_status == "unconfirmed":
        status = "unconfirmed"
        confidence = 30
        method = "lookup_failed"
        provider = "finder"
    elif email_status == "invalid":
        status = "invalid"
        confidence = 0
        method = "check_failed"
        provider = "dns"
    else:
        status = "unchecked"
        confidence = 20
        method = "none"
        provider = "none"

    return VerificationDetail(
        status=status,
        confidence=confidence,
        is_catch_all=is_catch_all,
        provider=provider,
        method=method,
        verified_at=now_iso,
        raw_details={"inferred": True}
    )


def evaluate_email_verification(detail: VerificationDetail, min_confidence: int = 80) -> tuple[bool, str]:
    """Evaluate whether the recipient email satisfies Gate B.

    Returns (passed: bool, reason: str).
    """
    if detail.status != "valid":
        return False, f"Recipient email status is '{detail.status}', requires 'valid'"

    eff = detail.effective_confidence()
    if detail.is_catch_all and eff < min_confidence:
        return False, (
            f"Catch-all mailbox without strong verification: effective confidence {eff}% "
            f"is below required threshold of {min_confidence}%"
        )

    if eff < min_confidence:
        return False, f"Recipient email confidence {eff}% is below required threshold of {min_confidence}%"

    return True, f"Recipient email verified ({detail.method or 'provider'}, {eff}% confidence)"
