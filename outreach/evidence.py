"""Structured evidence model and personalization safety verification.

Extracts, structures, and validates factual evidence supporting lead personalization.
Enforces that drafts only make factual claims supported by verified evidence.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field


class VerificationStatus:
    VERIFIED_FACT = "VERIFIED_FACT"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class FactType:
    TECH_STACK = "tech_stack"
    JOB_POSTING = "job_posting"
    COMPANY_NEWS = "company_news"
    PRODUCT_FEATURE = "product_feature"
    BUSINESS_MODEL = "business_model"
    HIRING_NEED = "hiring_need"
    OTHER = "other"


class EvidenceRecord(BaseModel):
    fact: str = Field(description="The factual claim or observation")
    fact_type: str = Field(default=FactType.OTHER, description="Category of fact")
    source_url: str | None = Field(default=None, description="URL where evidence was found, if available")
    source_type: str = Field(default="source", description="source | website | signals | news | public_match | manual")
    verification_status: str = Field(default=VerificationStatus.VERIFIED_FACT, description="VERIFIED_FACT | INFERENCE | UNKNOWN")
    confidence: float = Field(default=1.0, description="Confidence in the fact (0.0 - 1.0)")
    extracted_at: str | None = Field(default=None, description="ISO timestamp of extraction")


def _detect_fact_type(text: str) -> str:
    lower = text.lower()
    if any(k in lower for k in ("python", "react", "next.js", "node", "typescript", "crm", "hubspot", "n8n", "zapier", "make.com", "api", "whatsapp", "llm", "ai", "stack", "postgres", "sql")):
        return FactType.TECH_STACK
    if any(k in lower for k in ("hiring", "looking for", "job", "position", "role", "intern", "freelance", "contract", "salary", "budget")):
        return FactType.JOB_POSTING
    if any(k in lower for k in ("funding", "raised", "seed", "series", "launch", "announced", "acquired", "news")):
        return FactType.COMPANY_NEWS
    if any(k in lower for k in ("feature", "pricing", "integration", "dashboard", "portal", "platform", "app", "service")):
        return FactType.PRODUCT_FEATURE
    if any(k in lower for k in ("agency", "brokerage", "b2b", "saas", "client", "customer", "ecommerce", "real estate")):
        return FactType.BUSINESS_MODEL
    return FactType.OTHER


def extract_evidence_from_lead(lead: dict | Any) -> list[EvidenceRecord]:
    """Extract all available evidence records from a lead row or dict."""
    lead = dict(lead) if lead is not None else {}
    evidence: list[EvidenceRecord] = []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # 1. Existing verified_evidence column
    raw_ev = lead.get("verified_evidence")
    if raw_ev:
        try:
            items = json.loads(raw_ev) if isinstance(raw_ev, str) else raw_ev
            for it in items:
                if isinstance(it, dict) and it.get("fact"):
                    evidence.append(EvidenceRecord(
                        fact=it["fact"],
                        fact_type=it.get("fact_type", _detect_fact_type(it["fact"])),
                        source_url=it.get("source_url"),
                        source_type=it.get("source_type", "source"),
                        verification_status=it.get("verification_status", VerificationStatus.VERIFIED_FACT),
                        confidence=float(it.get("confidence", 1.0)),
                        extracted_at=it.get("extracted_at", now_iso)
                    ))
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. Research brief facts
    raw_brief = lead.get("research") if isinstance(lead, dict) else getattr(lead, "research", None)
    if raw_brief:
        try:
            brief = json.loads(raw_brief) if isinstance(raw_brief, str) else raw_brief
            for f in brief.get("facts", []):
                text = f.get("fact", "") if isinstance(f, dict) else str(f)
                src = f.get("source", "research") if isinstance(f, dict) else "research"
                if text:
                    evidence.append(EvidenceRecord(
                        fact=text,
                        fact_type=_detect_fact_type(text),
                        source_type=src,
                        verification_status=VerificationStatus.VERIFIED_FACT,
                        confidence=0.9,
                        extracted_at=now_iso
                    ))
            if brief.get("best_hook"):
                evidence.append(EvidenceRecord(
                    fact=brief["best_hook"],
                    fact_type=FactType.PRODUCT_FEATURE,
                    source_type="hook",
                    verification_status=VerificationStatus.VERIFIED_FACT,
                    confidence=0.85,
                    extracted_at=now_iso
                ))
            for p in brief.get("pains", []):
                if isinstance(p, dict) and p.get("evidence"):
                    evidence.append(EvidenceRecord(
                        fact=f"Pain evidence: {p['evidence']}",
                        fact_type=FactType.BUSINESS_MODEL,
                        source_type="pain_evidence",
                        verification_status=VerificationStatus.INFERENCE,
                        confidence=float(p.get("confidence", 0.7)),
                        extracted_at=now_iso
                    ))
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. Source text (directory posting / job posting)
    source_text = (lead.get("source_text") if isinstance(lead, dict) else getattr(lead, "source_text", None)) or ""
    if source_text.strip():
        # First 200 chars or meaningful sentences
        cleaned = " ".join(source_text.split()[:40])
        evidence.append(EvidenceRecord(
            fact=f"Source post: {cleaned}",
            fact_type=FactType.JOB_POSTING if any(k in source_text.lower() for k in ("hiring", "role", "job", "intern", "developer")) else FactType.OTHER,
            source_type="source_text",
            verification_status=VerificationStatus.VERIFIED_FACT,
            confidence=0.95,
            extracted_at=now_iso
        ))

    # 4. Signals
    signals_raw = lead.get("signals") if isinstance(lead, dict) else getattr(lead, "signals", None)
    if signals_raw:
        try:
            sigs = json.loads(signals_raw) if isinstance(signals_raw, str) else signals_raw
            if isinstance(sigs, dict):
                for k, v in sigs.items():
                    if v is True and k != "reachable":
                        evidence.append(EvidenceRecord(
                            fact=f"Detected site signal: {k}",
                            fact_type=FactType.TECH_STACK,
                            source_type="signals",
                            verification_status=VerificationStatus.VERIFIED_FACT,
                            confidence=0.9,
                            extracted_at=now_iso
                        ))
        except (json.JSONDecodeError, TypeError):
            pass

    # 5. Website match
    email_source = (lead.get("email_source") if isinstance(lead, dict) else getattr(lead, "email_source", None)) or ""
    if email_source in ("website", "public_match"):
        email = (lead.get("email") if isinstance(lead, dict) else getattr(lead, "email", None)) or ""
        evidence.append(EvidenceRecord(
            fact=f"Published contact email {email} verified on company website",
            fact_type=FactType.OTHER,
            source_type="public_match",
            verification_status=VerificationStatus.VERIFIED_FACT,
            confidence=1.0,
            extracted_at=now_iso
        ))

    # Deduplicate facts by text
    seen = set()
    deduped = []
    for ev in evidence:
        norm = ev.fact.strip().lower()
        if norm not in seen and len(norm) > 5:
            seen.add(norm)
            deduped.append(ev)

    return deduped


def filter_verified_facts(evidence: list[EvidenceRecord]) -> list[EvidenceRecord]:
    """Return only items that are VERIFIED_FACT with confidence >= 0.7."""
    return [
        e for e in evidence
        if e.verification_status == VerificationStatus.VERIFIED_FACT and e.confidence >= 0.7
    ]


def count_verified_facts(evidence: list[EvidenceRecord]) -> int:
    """Return the number of verified facts."""
    return len(filter_verified_facts(evidence))


# Common hallucinated assertions that should be caught
HALLUCINATED_METRIC_PATTERNS = [
    re.compile(r"\$\d+[\d,.]*(?:k|m|b| million| billion)?\s+(?:arr|revenue|funding|valuation|round|series\s+[a-z])\b", re.I),
    re.compile(r"\bseries\s+[a-z]\s+funding\b", re.I),
    re.compile(r"\b\d+\+?\s+employees\b", re.I),
    re.compile(r"\b\d+%\s+(?:drop|churn|increase|bounce)\b", re.I),
]


def validate_personalization_against_evidence(
    body: str,
    subject: str,
    evidence: list[EvidenceRecord]
) -> tuple[bool, str]:
    """Validate that drafted copy does not state ungrounded factual claims.

    Returns (is_valid, failure_reason).
    """
    full_text = f"{subject}\n{body}"

    # 1. Unfilled template placeholders
    placeholder_pattern = re.compile(
        r"YOUR[-_ ]?[A-Z]{3,}|(?i:\[(?:first[ _]?name|last[ _]?name|name|company|your [^\]]+)\]|"
        r"\{(?:first_?name|company|name)\})"
    )
    m = placeholder_pattern.search(full_text)
    if m:
        return False, f"Contains unfilled placeholder text: {m.group(0)!r}"

    # 2. Check for metric hallucinations without corresponding evidence
    verified = filter_verified_facts(evidence)
    all_evidence_text = " ".join(e.fact.lower() for e in verified)

    for pat in HALLUCINATED_METRIC_PATTERNS:
        match = pat.search(body)
        if match:
            metric_claim = match.group(0).lower()
            if metric_claim not in all_evidence_text:
                return False, f"Ungrounded metric claim {match.group(0)!r} not supported by verified evidence"

    return True, ""
