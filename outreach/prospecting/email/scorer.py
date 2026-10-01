"""Confidence scoring engine with configurable weights and deduction rules."""
from __future__ import annotations

from ..config import (
    get_weights,
    THRESHOLD_VERIFIED,
    THRESHOLD_HIGH,
    THRESHOLD_REVIEW
)
from ..models import CandidateEmail, Evidence


def classify_confidence(score: int) -> tuple[str, str]:
    """Map a 0-100 score to confidence level and default email status.
    
    Returns:
        (confidence_level, email_status)
    """
    if score >= THRESHOLD_VERIFIED:
        return "verified", "verified"
    if score >= THRESHOLD_HIGH:
        return "high_confidence", "high_confidence"
    if score >= THRESHOLD_REVIEW:
        return "uncertain", "uncertain"
    return "unresolved", "not_found"


def score_candidate(
    candidate: CandidateEmail,
    exact_public_match: bool,
    pattern_match: bool,
    multiple_employees: bool,
    valid_mx: bool,
    name_identity_match: bool,
    is_catch_all: bool,
    has_conflicting_patterns: bool,
    is_role: bool,
    public_url: str | None = None
) -> CandidateEmail:
    """Calculate the 0-100 confidence score for a candidate with granular evidence trail."""
    w = get_weights()
    score = 0
    evidence: list[Evidence] = list(candidate.evidence)

    # 1. Exact public evidence
    if exact_public_match:
        delta = w.get("exact_public_match", 45)
        score += delta
        evidence.append(Evidence(
            type="exact_public_match",
            detail=f"Exact public reference to '{candidate.email}' found online",
            url=public_url,
            weight=delta
        ))

    # 2. Confirmed company naming pattern
    if pattern_match:
        delta = w.get("confirmed_company_pattern", 25)
        score += delta
        evidence.append(Evidence(
            type="confirmed_company_pattern",
            detail=f"Matches company email pattern: {candidate.pattern}",
            weight=delta
        ))
        if multiple_employees:
            delta_multi = w.get("multiple_employees_pattern", 10)
            score += delta_multi
            evidence.append(Evidence(
                type="multiple_employees_pattern",
                detail="Multiple verified employee emails confirm this convention",
                weight=delta_multi
            ))
    else:
        # Generated from speculative pattern without company evidence
        penalty = w.get("unverified_pattern_penalty", -20)
        score += penalty
        evidence.append(Evidence(
            type="unverified_pattern_penalty",
            detail="Generated from industry fallback pattern with no company-specific proof",
            weight=penalty
        ))

    # 3. Valid MX records
    if valid_mx:
        delta = w.get("valid_mx", 10)
        score += delta
        evidence.append(Evidence(
            type="valid_mx",
            detail="Domain has active, valid MX mail servers",
            weight=delta
        ))
    else:
        evidence.append(Evidence(
            type="missing_mx",
            detail="Domain lacks active MX records (unlikely to receive mail)",
            weight=0
        ))

    # 4. Name identity match
    if name_identity_match:
        delta = w.get("name_identity_match", 10)
        score += delta
        evidence.append(Evidence(
            type="name_identity_match",
            detail="Localpart distinctly matches target prospect's full name",
            weight=delta
        ))

    # 5. Penalties
    if is_catch_all:
        penalty = w.get("catch_all_penalty", -15)
        score += penalty
        evidence.append(Evidence(
            type="catch_all_penalty",
            detail="Domain appears to be catch-all; individual mailbox deliverability unconfirmed",
            weight=penalty
        ))

    if has_conflicting_patterns:
        penalty = w.get("conflicting_patterns_penalty", -10)
        score += penalty
        evidence.append(Evidence(
            type="conflicting_patterns_penalty",
            detail="Discovered company emails display contradictory naming schemes",
            weight=penalty
        ))

    if is_role:
        penalty = w.get("role_address_penalty", -30)
        score += penalty
        evidence.append(Evidence(
            type="role_address_penalty",
            detail="Email is a shared role mailbox (info, sales, support) rather than a decision maker",
            weight=penalty
        ))

    # Clamp between 0 and 100
    final_score = max(0, min(100, score))
    candidate.score = final_score
    candidate.evidence = evidence
    return candidate
