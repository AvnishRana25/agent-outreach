"""Email patterns, generation and scoring package."""
from .patterns import (
    SUPPORTED_PATTERNS,
    DEFAULT_PATTERN_PRIORITIES,
    format_pattern,
    clean_name_token,
    detect_company_pattern,
    deduce_pattern_from_email,
)
from .generator import generate_candidates
from .scorer import score_candidate, classify_confidence

__all__ = [
    "SUPPORTED_PATTERNS",
    "DEFAULT_PATTERN_PRIORITIES",
    "format_pattern",
    "clean_name_token",
    "detect_company_pattern",
    "deduce_pattern_from_email",
    "generate_candidates",
    "score_candidate",
    "classify_confidence",
]
