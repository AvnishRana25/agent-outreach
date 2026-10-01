"""Generate and prioritize email candidates for a prospect."""
from __future__ import annotations

from .patterns import (
    SUPPORTED_PATTERNS,
    DEFAULT_PATTERN_PRIORITIES,
    format_pattern,
    clean_name_token
)
from ..models import CandidateEmail, Evidence


def generate_candidates(
    first_name: str,
    last_name: str,
    domain: str,
    preferred_pattern: str | None = None
) -> list[CandidateEmail]:
    """Generate all supported email permutations, ordered with preferred pattern first."""
    f = clean_name_token(first_name)
    l = clean_name_token(last_name)
    dom = domain.strip().lower()

    if not dom or (not f and not l):
        return []

    # Order patterns: preferred pattern first, then remaining standard patterns
    patterns_order: list[str] = []
    if preferred_pattern and preferred_pattern in SUPPORTED_PATTERNS:
        patterns_order.append(preferred_pattern)
    for p in DEFAULT_PATTERN_PRIORITIES:
        if p not in patterns_order:
            patterns_order.append(p)

    seen_emails: set[str] = set()
    candidates: list[CandidateEmail] = []

    for pat in patterns_order:
        addr = format_pattern(pat, f, l, dom)
        if addr and addr not in seen_emails:
            seen_emails.add(addr)
            ev = []
            if preferred_pattern and pat == preferred_pattern:
                ev.append(Evidence(
                    type="pattern_order",
                    detail=f"Prioritized by confirmed company pattern: {pat}",
                    weight=0
                ))
            candidates.append(CandidateEmail(
                email=addr,
                pattern=pat,
                evidence=ev
            ))

    return candidates
