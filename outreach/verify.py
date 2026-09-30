"""Free email checks: syntax, MX records, role/disposable addresses, and pattern guessing.

This does NOT do an SMTP mailbox probe (most home/cloud IPs have port 25 blocked, and probing
from your sending IP hurts its reputation). For leads that matter, run the CSV through a
verifier's free credits (see PLAYBOOK.md) and import the result with email_status set.
Bounce rate above ~3% is what gets new domains flagged, so 'guessed' emails are never sent
unless you pass --allow-guessed.
"""
from __future__ import annotations

import re
from functools import lru_cache

import dns.exception
import dns.resolver

from . import config, db
from .enrich import score

SYNTAX = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
ROLE = {"info", "contact", "hello", "sales", "admin", "office", "enquiry", "enquiries",
        "support", "team", "mail", "hr", "careers", "jobs", "marketing", "help"}
# Addresses that are placeholders, or inboxes nobody reads for sales.
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "email.com", "yourdomain.com",
                       "yoursite.com", "company.com", "test.com", "sentry.io", "wixpress.com", "mysite.com"}
JUNK_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "privacy", "legal", "abuse", "billing",
              "accounts", "accounting", "invoice", "invoices", "payroll", "webmaster", "postmaster",
              "hostmaster", "dpo", "gdpr", "unsubscribe", "example", "email", "name", "your", "yourname"}


def clean(raw: str) -> str:
    """Undo scraping artefacts: '%20hello@x.com', 'u003esupport@x.com' (JSON-escaped '>'), 'mailto:'."""
    import html
    from urllib.parse import unquote
    e = unquote(html.unescape(raw or "")).strip().strip("<>\"'.,;:()[]").lower()
    e = re.sub(r"^(mailto:|u003[ce]|x3[ce]|\\+)+", "", e)
    m = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", e)
    return m.group(0) if m else ""


DISPOSABLE = {"mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
              "yopmail.com", "trashmail.com"}


@lru_cache(maxsize=4096)
def has_mx(domain: str) -> bool:
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=8))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False
    except dns.exception.Timeout:
        return True  # don't throw a lead away over a slow resolver


def check(email: str) -> str:
    email = (email or "").lower().strip()
    if not SYNTAX.match(email):
        return "invalid"
    local, domain = email.split("@")
    if (domain in PLACEHOLDER_DOMAINS or domain.endswith((".example", ".test", ".invalid", ".local"))
            or local.split("+")[0] in JUNK_LOCAL):
        return "invalid"
    if domain in DISPOSABLE or not has_mx(domain):
        return "invalid"
    if local in ROLE:
        # Fine for 3-10 person brokerages/agencies where info@ is read by the owner,
        # but a named person is always better.
        return "risky"
    return "valid"


def guesses(first: str, last: str, domain: str) -> list[str]:
    f, l = first.lower().strip(), last.lower().strip()
    if not f or not domain:
        return []
    out = [f"{f}@{domain}"]
    if l:
        out = [f"{f}.{l}@{domain}", f"{f}@{domain}", f"{f}{l}@{domain}", f"{f[0]}{l}@{domain}"]
    return out


def run() -> dict:
    counts = {"valid": 0, "risky": 0, "invalid": 0, "guessed": 0, "no_email": 0}
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM leads WHERE status = 'enriched'").fetchall()
        for row in rows:
            email = clean(row["email"] or "") or None
            if email != row["email"]:
                if email and conn.execute("SELECT 1 FROM leads WHERE email=? AND id!=?", (email, row["id"])).fetchone():
                    db.set_lead(conn, row["id"], status="rejected", notes=(row["notes"] or "") + " | duplicate email")
                    continue
                db.set_lead(conn, row["id"], email=email, email_status="unchecked")
                row = conn.execute("SELECT * FROM leads WHERE id=?", (row["id"],)).fetchone()
            if not email:
                g = guesses(row["first_name"] or "", row["last_name"] or "", row["domain"] or "")
                if g and has_mx(row["domain"]):
                    db.set_lead(conn, row["id"], email=g[0], email_status="guessed", status="verified")
                    counts["guessed"] += 1
                else:
                    db.set_lead(conn, row["id"], status="invalid", email_status="invalid")
                    counts["no_email"] += 1
                continue
            if db.suppressed(conn, email):
                db.set_lead(conn, row["id"], status="rejected")
                continue
            status = row["email_status"] if row["email_status"] not in ("unchecked", "", None) else check(email)
            seg = config.segment(row["segment"])
            if status == "invalid":
                db.set_lead(conn, row["id"], email_status=status, status="invalid")
            else:
                db.set_lead(conn, row["id"], email_status=status, status="verified",
                            score=score(seg, db.signals(row), status))
            counts[status] = counts.get(status, 0) + 1
    return counts
