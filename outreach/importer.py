"""Import leads from CSV (Google Maps exports, Apollo/Hunter exports, hand-built sheets).

Recognised columns (case-insensitive; other columns are ignored):
email, first_name, last_name, name, title, company, website, country, city, linkedin, notes, segment
"""
from __future__ import annotations

import csv
from pathlib import Path
from urllib.parse import urlparse

from . import config, db

ALIASES = {
    "email": ["email", "email address", "work email", "emails"],
    "first_name": ["first_name", "first name", "firstname"],
    "last_name": ["last_name", "last name", "lastname"],
    "name": ["name", "full name", "contact name", "owner"],
    "title": ["title", "job title", "designation", "role"],
    "company": ["company", "company name", "organization", "business name", "agency"],
    "website": ["website", "company website", "url", "domain", "site"],
    "country": ["country"],
    "city": ["city", "location"],
    "linkedin": ["linkedin", "linkedin url", "person linkedin url"],
    "notes": ["notes", "note", "hook", "trigger"],
    "segment": ["segment"],
}


def _pick(row: dict, field: str) -> str:
    lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
    for alias in ALIASES[field]:
        if lowered.get(alias):
            return lowered[alias]
    return ""


FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
             "icloud.com", "proton.me", "protonmail.com", "aol.com", "zoho.com", "zohomail.in",
             "yahoo.co.in", "rediffmail.com"}


def domain_of(website: str, email: str = "") -> str:
    """Company domain used for de-duplication; empty for personal mailbox providers."""
    if website:
        url = website if "://" in website else "https://" + website
        return urlparse(url).netloc.lower().removeprefix("www.").split(":")[0]
    if "@" in email:
        dom = email.split("@")[1].lower()
        return "" if dom in FREE_MAIL else dom
    return ""


def import_csv(path: Path, default_segment: str | None, source: str) -> tuple[int, int]:
    added = skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as f, db.connect() as conn:
        for row in csv.DictReader(f):
            seg = _pick(row, "segment") or default_segment
            if not seg:
                raise SystemExit("No segment column and no --segment given")
            config.segment(seg)  # validates the name

            email = _pick(row, "email").split(",")[0].strip().lower()
            first, last = _pick(row, "first_name"), _pick(row, "last_name")
            if not first and _pick(row, "name"):
                parts = _pick(row, "name").split()
                first, last = parts[0], " ".join(parts[1:])
            website = _pick(row, "website")
            if not email and not website:
                skipped += 1
                continue
            if email and db.suppressed(conn, email):
                skipped += 1
                continue
            dom = domain_of(website, email)
            ok = db.add_lead(
                conn, first_name=first.title(), last_name=last.title(), title=_pick(row, "title"),
                company=_pick(row, "company"), website=website, domain=dom, email=email or None,
                email_source="csv" if email else "",
                country=_pick(row, "country") or config.segment(seg).get("country", ""),
                city=_pick(row, "city"), linkedin=_pick(row, "linkedin"), segment=seg,
                source=source, notes=_pick(row, "notes"))
            added, skipped = (added + 1, skipped) if ok else (added, skipped + 1)
    return added, skipped
