"""Syntax, role-address, and disposable email validation."""
from __future__ import annotations

import re

EMAIL_REGEX = re.compile(
    r"^(?!\.)(?!.*\.\.)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$"
)

ROLE_PREFIXES = {
    "info", "support", "hello", "sales", "admin", "contact", "enquiry", "enquiries",
    "team", "help", "billing", "office", "hr", "careers", "jobs", "marketing",
    "media", "press", "inbox", "mail", "general", "compliance", "privacy", "legal",
    "accounting", "invoices", "accounts", "feedback", "service", "customer"
}

DISPOSABLE_DOMAINS = {
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getairmail.com",
    "dispostable.com", "throwawaymail.com", "fakemailgenerator.com"
}


def is_valid_syntax(email: str | None) -> bool:
    """Validate RFC 5322 compliance and basic constraints."""
    if not email:
        return False
    e = email.strip().lower()
    if len(e) > 254:
        return False
    parts = e.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if len(local) > 64 or not local or not domain:
        return False
    return bool(EMAIL_REGEX.match(e))


def is_role_email(email: str | None) -> bool:
    """Check if the email is a shared, generic or role-based mailbox."""
    if not email or "@" not in email:
        return False
    local = email.strip().lower().split("@")[0].split("+")[0]
    return local in ROLE_PREFIXES


def is_disposable_domain(domain: str | None) -> bool:
    """Check if the domain belongs to known disposable/temporary email services."""
    if not domain:
        return False
    dom = domain.strip().lower().removeprefix("www.")
    return dom in DISPOSABLE_DOMAINS
