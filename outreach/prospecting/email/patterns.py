"""Email naming pattern definitions, matching and company convention inference."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any

# Supported pattern identifiers
SUPPORTED_PATTERNS = [
    "first.last",
    "first",
    "flast",
    "firstlast",
    "f.last",
    "first_last",
    "first-last",
    "firstl",
    "last",
    "last.first",
]

# Standard industry distribution fallback when no company pattern is found
DEFAULT_PATTERN_PRIORITIES = [
    "first.last",
    "first",
    "flast",
    "firstlast",
    "f.last",
    "first_last",
    "first-last",
    "firstl",
    "last",
    "last.first",
]


def clean_name_token(name: str) -> str:
    """Normalize a name token by removing accents, spaces, and punctuation."""
    if not name:
        return ""
    # Strip diacritics / accents (e.g., José -> Jose)
    nfkd = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", stripped).lower()
    return cleaned


def format_pattern(pattern: str, first: str, last: str, domain: str) -> str:
    """Render a pattern template into an email address."""
    f = clean_name_token(first)
    l = clean_name_token(last)
    dom = domain.strip().lower()

    if not f and not l:
        return ""

    # Single name fallbacks
    if not l:
        return f"{f}@{dom}" if f else ""
    if not f:
        return f"{l}@{dom}" if l else ""

    f_initial = f[0]
    l_initial = l[0]

    templates = {
        "first.last": f"{f}.{l}@{dom}",
        "first": f"{f}@{dom}",
        "flast": f"{f_initial}{l}@{dom}",
        "firstlast": f"{f}{l}@{dom}",
        "f.last": f"{f_initial}.{l}@{dom}",
        "first_last": f"{f}_{l}@{dom}",
        "first-last": f"{f}-{l}@{dom}",
        "firstl": f"{f}{l_initial}@{dom}",
        "last": f"{l}@{dom}",
        "last.first": f"{l}.{f}@{dom}",
    }
    return templates.get(pattern, f"{f}.{l}@{dom}")


def deduce_pattern_from_email(local: str, name_hints: tuple[str, str] | None = None) -> str | None:
    """Infer which pattern an existing email localpart corresponds to."""
    local = clean_name_token(local.replace(".", "DOT").replace("_", "UND").replace("-", "HYP"))
    local = local.replace("dot", ".").replace("und", "_").replace("hyp", "-").lower()

    if name_hints and name_hints[0] and name_hints[1]:
        f = clean_name_token(name_hints[0])
        l = clean_name_token(name_hints[1])
        if f and l:
            if local == f"{f}.{l}":
                return "first.last"
            if local == f"{f}_{l}":
                return "first_last"
            if local == f"{f}-{l}":
                return "first-last"
            if local == f"{f[0]}.{l}":
                return "f.last"
            if local == f"{f[0]}{l}":
                return "flast"
            if local == f"{f}{l}":
                return "firstlast"
            if local == f"{f}{l[0]}":
                return "firstl"
            if local == f:
                return "first"
            if local == l:
                return "last"
            if local == f"{l}.{f}":
                return "last.first"

    # Structural deduction without explicit name
    if "." in local:
        parts = local.split(".")
        if len(parts) == 2:
            return "f.last" if len(parts[0]) == 1 else "first.last"
    if "_" in local:
        return "first_last"
    if "-" in local:
        return "first-last"

    return None


def detect_company_pattern(known_emails: list[str], domain: str) -> dict[str, Any]:
    """Analyze a list of known employee emails at a domain to find the dominant naming convention.
    
    Returns:
        {
            "dominant_pattern": str | None,
            "pattern_confidence": float (0.0 - 1.0),
            "match_count": int,
            "counts": dict[str, int],
            "known_emails": list[str]
        }
    """
    valid_locals: list[str] = []
    dom_clean = domain.strip().lower()

    for e in known_emails:
        if "@" not in e:
            continue
        loc, d = e.split("@", 1)
        if d.lower() == dom_clean:
            valid_locals.append(loc.strip().lower())

    if not valid_locals:
        return {
            "dominant_pattern": None,
            "pattern_confidence": 0.0,
            "match_count": 0,
            "counts": {},
            "known_emails": []
        }

    inferred_patterns: list[str] = []
    for loc in valid_locals:
        p = deduce_pattern_from_email(loc)
        if p:
            inferred_patterns.append(p)

    counts = Counter(inferred_patterns)
    if not counts:
        return {
            "dominant_pattern": None,
            "pattern_confidence": 0.0,
            "match_count": 0,
            "counts": {},
            "known_emails": [f"{loc}@{dom_clean}" for loc in valid_locals]
        }

    top_pattern, top_count = counts.most_common(1)[0]
    total_deduced = sum(counts.values())
    confidence = round(top_count / total_deduced, 2)

    return {
        "dominant_pattern": top_pattern,
        "pattern_confidence": confidence,
        "match_count": top_count,
        "counts": dict(counts),
        "known_emails": [f"{loc}@{dom_clean}" for loc in valid_locals]
    }
