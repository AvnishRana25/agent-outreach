"""Visit each lead's website and pull out the signals the email will be personalised on.

The personalisation is only as good as these signals, so this is where "highly personalised"
actually comes from: what the business sells, how leads reach it today (WhatsApp link,
contact form, chat widget, property portals), what tools it already runs, and whether it is hiring.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from . import db

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
EXTRA_PATHS = ["/about", "/about-us", "/contact", "/contact-us", "/services", "/careers", "/team"]

# name -> regex searched in raw HTML
DETECTORS = {
    "whatsapp_link": r"wa\.me/|api\.whatsapp\.com|whatsapp://",
    "whatsapp_tool": r"wati\.io|interakt|aisensy|gallabox|doubletick|zoko|respond\.io|twilio",
    "chat_widget": r"tawk\.to|intercom|crisp\.chat|drift\.com|zendesk|freshchat|tidio|livechatinc",
    "crm": r"hubspot|zoho\.com/crm|salesforce|pipedrive|leadsquared|freshsales|sell\.do|kylas|bitrix",
    "form_tool": r"typeform|docs\.google\.com/forms|jotform|tally\.so|wpforms|contact-form-7|elementor-form",
    "booking": r"calendly|cal\.com|youcanbook|zcal",
    "analytics": r"googletagmanager|gtag\(|fbq\(|facebook\.net/.*/fbevents",
    "portals": r"99acres|magicbricks|housing\.com|nobroker|squareyards|bayut|propertyfinder|dubizzle"
               r"|rightmove|zoopla|onthemarket|zillow|realtor\.com|realestate\.com\.au|domain\.com\.au",
    "cms_wordpress": r"wp-content|wordpress",
    "cms_wix": r"wix\.com|wixstatic",
    "cms_shopify": r"cdn\.shopify|myshopify",
    "framework_next": r"__next|_next/static",
    "careers": r"careers|we(?:'|\u2019| a)?re hiring|join our team|open (?:positions|roles)",
    "ai_mention": r"\bAI\b|artificial intelligence|machine learning|LLM|GPT",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _fetch(url: str) -> str:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except requests.RequestException:
        pass
    return ""


def _visible_text(html: str, limit: int) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    return text[:limit]


def analyse(website: str) -> tuple[dict, str]:
    base = website if "://" in website else "https://" + website
    home = _fetch(base)
    if not home:
        return {"reachable": False}, ""
    pages = {"home": home}
    for path in EXTRA_PATHS:
        html = _fetch(urljoin(base, path))
        if html:
            pages[path] = html
    raw = "\n".join(pages.values())

    soup = BeautifulSoup(home, "html.parser")
    meta = soup.find("meta", attrs={"name": "description"})
    found = {k: bool(re.search(p, raw, re.I)) for k, p in DETECTORS.items()}
    emails = sorted({e.lower() for e in EMAIL_RE.findall(raw)
                     if not e.lower().endswith((".png", ".jpg", ".webp", ".svg", ".gif"))})
    sig = {
        "reachable": True,
        "title": (soup.title.string or "").strip()[:150] if soup.title else "",
        "meta_description": (meta.get("content", "") if meta else "")[:300],
        "headings": [h.get_text(" ", strip=True)[:120] for h in soup.find_all(["h1", "h2"])][:8],
        "pages_found": list(pages),
        "emails_on_site": emails[:5],
        **found,
    }
    text = " | ".join(f"[{name}] {_visible_text(html, 1500)}" for name, html in pages.items())
    return sig, text[:5000]


def score(segment_cfg: dict, sig: dict, email_status: str) -> int:
    """Rough priority so the best-fit leads get drafted first each day."""
    s = 0
    for key, weight in (segment_cfg.get("score_signals") or {}).items():
        if key.startswith("no_"):
            s += weight if not sig.get(key[3:]) else 0
        elif sig.get(key):
            s += weight
    s += {"valid": 20, "risky": 5, "guessed": 0}.get(email_status, 0)
    return s


def run(limit: int = 100) -> int:
    done = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status = 'new' AND website != '' LIMIT ?", (limit,)
        ).fetchall()
    for row in rows:
        sig, text = analyse(row["website"])
        with db.connect() as conn:
            fields = {"signals": json.dumps(sig), "site_text": text, "status": "enriched"}
            if not row["email"] and sig.get("emails_on_site"):
                fields["email"] = _best_email(sig["emails_on_site"], row["domain"])
                fields["email_source"] = "website"
            if not sig.get("reachable"):
                fields["notes"] = ((row["notes"] or "") + " | website unreachable").strip(" |")
            db.set_lead(conn, row["id"], **fields)
        done += 1
        print(f"  enriched {row['company'] or row['domain']}: "
              f"{', '.join(k for k, v in sig.items() if v is True) or 'no signals'}")
    # Leads with no website still move on, they just get less personalisation.
    with db.connect() as conn:
        conn.execute("UPDATE leads SET status='enriched' WHERE status='new' AND (website IS NULL OR website='')")
    return done


def _best_email(emails: list[str], domain: str) -> str:
    on_domain = [e for e in emails if domain and e.endswith("@" + domain)] or emails
    generic = {"noreply", "no-reply", "privacy", "info", "contact", "hello", "sales", "admin",
               "office", "enquiry", "enquiries", "support", "team", "mail", "hr", "careers", "jobs"}
    named = [e for e in on_domain if e.split("@")[0] not in generic]
    return (named or on_domain)[0]
