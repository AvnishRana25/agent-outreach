"""Validation package for email, DNS, MX and catch-all checks."""
from .syntax import is_valid_syntax, is_role_email, is_disposable_domain
from .mx import check_mx, detect_provider
from .catch_all import check_catch_all

__all__ = [
    "is_valid_syntax", "is_role_email", "is_disposable_domain",
    "check_mx", "detect_provider", "check_catch_all"
]
