"""Pydantic models for B2B email prospecting pipeline."""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    """An individual piece of evidence supporting or penalizing an email candidate."""
    type: str = Field(description="Evidence type (e.g. public_match, company_pattern, mx, role_penalty)")
    detail: str = Field(description="Human-readable explanation of the evidence")
    url: str | None = Field(default=None, description="Source URL where evidence was found, if any")
    weight: int = Field(default=0, description="Score delta contributed by this evidence")


class CandidateEmail(BaseModel):
    """A generated email candidate for a prospect."""
    email: str
    pattern: str
    score: int = 0
    evidence: list[Evidence] = Field(default_factory=list)


class ProspectInput(BaseModel):
    """Input prospect record to be enriched."""
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str | None = None
    title: str | None = None
    linkedin_url: str | None = None

    @property
    def full_name(self) -> str:
        parts = [p.strip() for p in (self.first_name, self.last_name) if p.strip()]
        return " ".join(parts)


class ProviderResult(BaseModel):
    """Result returned by an external fallback provider."""
    provider: str
    email: str | None = None
    confidence: int = 0
    status: str = "not_found"
    credits_used: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)


class ProspectResult(BaseModel):
    """Complete result of prospecting enrichment with full provenance."""
    id: int | None = None
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    domain: str = ""
    title: str = ""
    linkedin_url: str = ""

    candidate_email: str | None = None
    final_email: str | None = None

    email_pattern: str | None = None
    confidence_score: int = 0
    confidence_level: str = "unresolved"  # verified | high_confidence | inferred | uncertain | unresolved

    email_status: str = "new"  # verified | high_confidence | inferred | uncertain | manual_review | not_found | invalid | do_not_contact | opted_out | bounced
    source: str = ""
    source_url: str | None = None

    mx_valid: bool = False
    catch_all: bool = False
    role_email: bool = False

    verification_provider: str | None = None
    verification_result: str = ""
    evidence: list[Evidence] = Field(default_factory=list)

    created_at: str | None = None
    updated_at: str | None = None
    last_checked_at: str | None = None
