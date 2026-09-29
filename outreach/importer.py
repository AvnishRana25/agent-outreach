"""Import leads from CSV (Google Maps exports, Apollo/Hunter exports, hand-built sheets).

Recognised columns (case-insensitive; other columns are ignored):
email, first_name, last_name, name, title, company, website, country, city, linkedin, notes, segment
"""
from __future__ import annotations

import csv
import sqlite3
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


def domain_of(website: str, email: str = "") -> str:
    if website:
        url = website if "://" in website else "https://" + website
        host = urlparse(url).netloc.lower()
        return host.removeprefix("www.")
    if "@" in email:
        return email.split("@")[1].lower()
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
            # One lead per company domain keeps us from emailing 3 people at a 5-person firm.
            if dom and conn.execute("SELECT 1 FROM leads WHERE domain = ?", (dom,)).fetchone():
                skipped += 1
                continue
            try:
                conn.execute(
                    """INSERT INTO leads (email, first_name, last_name, title, company, website,
                       domain, country, city, linkedin, segment, source, notes, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        email or None, first.title(), last.title(), _pick(row, "title"),
                        _pick(row, "company"), website, dom,
                        _pick(row, "country") or config.segment(seg).get("country", ""),
                        _pick(row, "city"), _pick(row, "linkedin"), seg, source,
                        _pick(row, "notes"), db.now(), db.now(),
                    ),
                )
                added += 1
            except sqlite3.IntegrityError:  # duplicate email
                skipped += 1
    return added, skipped
