"""Catch-all domain detection with safe optional probe."""
from __future__ import annotations

import os
import smtplib
import uuid

_CATCH_ALL_CACHE: dict[str, tuple[bool, str]] = {}


def check_catch_all(domain: str, mx_hosts: list[str]) -> tuple[bool, str]:
    """Check if domain accepts emails to arbitrary non-existent addresses (catch-all).
    
    Returns:
        (is_catch_all: bool, method: str)
    """
    if not domain or not mx_hosts:
        return False, "no_mx"

    dom = domain.strip().lower()
    if dom in _CATCH_ALL_CACHE:
        return _CATCH_ALL_CACHE[dom]

    # Check if optional SMTP probe is explicitly enabled
    enable_probe = os.getenv("ENABLE_SMTP_PROBE", "").lower() in ("true", "1", "yes")

    if not enable_probe:
        # Without active SMTP probing, major managed suites (Workspace, M365) default to non-catch-all
        provider_blob = " ".join(mx_hosts).lower()
        if any(known in provider_blob for known in ("google.com", "outlook.com", "zoho.")):
            res = (False, "inferred_provider_default")
        else:
            res = (False, "untested_probe_disabled")
        _CATCH_ALL_CACHE[dom] = res
        return res

    # Optional, isolated probe behind feature flag (clearly marked as unreliable/heuristic)
    random_user = f"nonexistent_{uuid.uuid4().hex[:10]}"
    test_email = f"{random_user}@{dom}"
    primary_mx = mx_hosts[0]

    try:
        with smtplib.SMTP(primary_mx, 25, timeout=5.0) as smtp:
            smtp.helo("check.agent-outreach.local")
            smtp.mail("probe@agent-outreach.local")
            code, _ = smtp.rcpt(test_email)
            if code == 250:
                # Server accepted random nonexistent address -> catch-all!
                res = (True, "smtp_probe_accepted_random_unreliable")
            else:
                res = (False, "smtp_probe_rejected_random_unreliable")
            _CATCH_ALL_CACHE[dom] = res
            return res
    except Exception as e:
        res = (False, f"smtp_probe_error: {type(e).__name__}")
        _CATCH_ALL_CACHE[dom] = res
        return res
