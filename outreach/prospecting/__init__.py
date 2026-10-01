"""B2B Email Prospecting Pipeline.

Discovers professional business email addresses for founders, CEOs, and B2B decision-makers
through public domain evidence, pattern inference, local DNS/MX validation, and optional
fallback credit providers.
"""
from .models import ProspectInput, ProspectResult, Evidence, CandidateEmail
from .pipeline.processor import ProspectProcessor
from .pipeline.bulk import BulkProcessor, load_prospects_from_csv
from .domain.normalizer import normalize_domain
from .domain.resolver import resolve_domain

__all__ = [
    "ProspectInput",
    "ProspectResult",
    "Evidence",
    "CandidateEmail",
    "ProspectProcessor",
    "BulkProcessor",
    "load_prospects_from_csv",
    "normalize_domain",
    "resolve_domain",
]
