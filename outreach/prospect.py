"""Automated prospecting from free, public, ToS-friendly sources. No LinkedIn scraping.

Sources:
  yc    YC company directory (yc-oss open JSON): startups that are hiring, filtered by region/size.
  hn    Hacker News monthly "Who is hiring?" and "Seeking freelancer?" threads (Algolia API).
        Posters publish an email so people will contact them about the role or project.
  osm   OpenStreetMap (Overpass API): businesses by category in a city bounding box,
        e.g. estate agents in Dubai or marketing agencies in London, with website/email tags.
  csv   `outreach import` for anything you collect yourself.

Each job in settings.yaml -> prospecting says which source feeds which segment and how many
new leads it may add per run. Duplicate companies (by domain) are skipped automatically.
"""
from __future__ import annotations

import html
import re
import time

import requests

from . import config, db
from .importer import domain_of

UA = {"User-Agent": "agent-outreach/1.0 (personal prospect research; contact: see sender profile)"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')]+")
SKIP_DOMAINS = ("ycombinator.com", "news.ycombinator.com", "github.com", "linkedin.com",
                "twitter.com", "x.com", "google.com", "notion.site", "lever.co", "greenhouse.io",
                "ashbyhq.com", "workable.com", "wellfound.com", "angel.co", "calendly.com")


def _get_json(url: str, **kw):
    r = requests.get(url, headers=UA, timeout=60, **kw)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- YC
YC_ALL = "https://yc-oss.github.io/api/companies/all.json"


def yc_candidates(data: list[dict], regions: list[str], exclude_regions: list[str],
                  max_team: int, min_batch_year: int) -> list[dict]:
    out = []
    for c in data:
        if not c.get("isHiring") or c.get("status") not in (None, "Active"):
            continue
        team = c.get("team_size") or 0
        if team == 0 or team > max_team:
            continue
        regs = set(c.get("regions") or []) | {c.get("all_locations") or ""}
        text_regs = " ".join(regs)
        if regions and not any(r.lower() in text_regs.lower() for r in regions):
            continue
        if exclude_regions and any(r.lower() in text_regs.lower() for r in exclude_regions):
            continue
        year = re.search(r"(\d{4})", c.get("batch") or "")
        if year and int(year.group(1)) < min_batch_year:
            continue
        if not c.get("website"):
            continue
        out.append(c)
    return out


def _yc_founders(slug: str) -> list[str]:
    """Best effort: founder names from the public YC company page."""
    try:
        page = requests.get(f"https://www.ycombinator.com/companies/{slug}", headers=UA, timeout=20).text
    except requests.RequestException:
        return []
    names = re.findall(r'full_name&quot;:&quot;([^&]+)&quot;', page) or re.findall(r'"full_name":"([^"]+)"', page)
    return list(dict.fromkeys(names))


def run_yc(job: dict) -> int:
    data = _get_json(YC_ALL)
    cands = yc_candidates(data, job.get("regions", []), job.get("exclude_regions", []),
                          job.get("max_team", 60), job.get("min_batch_year", 2021))
    added = 0
    with db.connect() as conn:
        for c in cands:
            if added >= job.get("max_new", 20):
                break
            dom = domain_of(c["website"])
            if not dom or conn.execute("SELECT 1 FROM leads WHERE domain=?", (dom,)).fetchone():
                continue
            founders = _yc_founders(c.get("slug", "")) if job.get("fetch_founders", True) else []
            first, last = (founders[0].split(" ", 1) + [""])[:2] if founders else ("", "")
            text = (f"YC {c.get('batch')} | team {c.get('team_size')} | {', '.join(c.get('regions') or [])}\n"
                    f"{c.get('one_liner', '')}\n{c.get('long_description', '')}\n"
                    f"Industry: {c.get('industry')} / tags: {', '.join(c.get('tags') or [])}\n"
                    f"Founders: {', '.join(founders) or 'unknown'}")
            if db.add_lead(conn, first_name=first, last_name=last, title="Founder" if founders else "",
                           company=c.get("name"), website=c["website"], domain=dom,
                           country=_first_country(c.get("regions") or []), segment=job["segment"],
                           source="yc", source_text=text[:3000]):
                added += 1
            time.sleep(0.5)
    return added


def _first_country(regions: list[str]) -> str:
    for r in regions:
        if r not in ("Remote", "America / Canada", "Europe", "Latin America", "South Asia",
                     "Southeast Asia", "Africa", "Middle East and North Africa", "Oceania", "East Asia"):
            return r
    return regions[0] if regions else ""


# --------------------------------------------------------------------------- Hacker News
HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=12"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
THREAD_TITLES = {"hiring": "who is hiring", "freelance": "seeking freelancer"}


def _clean(text: str) -> str:
    text = re.sub(r"<p>", "\n", text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).strip()


def deobfuscate(text: str) -> str:
    t = re.sub(r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*", "@", text, flags=re.I)
    t = re.sub(r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*", ".", t, flags=re.I)
    t = re.sub(r"(\w)\s+at\s+(\w[\w-]*)\s+dot\s+(\w+)", r"\1@\2.\3", t, flags=re.I)
    return t


def parse_hn_post(text: str) -> dict:
    clean = _clean(text)
    first_line = clean.splitlines()[0] if clean else ""
    parts = [p.strip() for p in first_line.split("|")]
    emails = [e.lower().rstrip(".") for e in EMAIL_RE.findall(deobfuscate(clean))]
    urls = [u.rstrip(".,") for u in URL_RE.findall(html.unescape(text or ""))]
    site = next((u for u in urls if not any(s in u for s in SKIP_DOMAINS)), "")
    company = parts[0] if parts else ""
    if company.upper().startswith(("SEEKING FREELANCER", "SEEKING WORK")):
        company = parts[1] if len(parts) > 1 else ""
    return {"company": company[:80], "email": emails[0] if emails else "", "website": site,
            "text": clean, "remote": bool(re.search(r"\bremote\b", clean, re.I))}


def run_hn(job: dict) -> int:
    kind = job.get("thread", "hiring")
    hits = _get_json(HN_SEARCH)["hits"]
    story = next((h for h in hits if THREAD_TITLES[kind] in (h.get("title") or "").lower()), None)
    if not story:
        print(f"  no recent HN '{kind}' thread found")
        return 0
    item = _get_json(HN_ITEM.format(id=story["objectID"]))
    include = [k.lower() for k in job.get("include_any", [])]
    include_re = re.compile(job["include_regex"], re.I) if job.get("include_regex") else None
    exclude = [k.lower() for k in job.get("exclude_any", [])]
    added = 0
    with db.connect() as conn:
        for child in item.get("children") or []:
            if added >= job.get("max_new", 20) or not child.get("text"):
                continue
            post = parse_hn_post(child["text"])
            low = post["text"].lower()
            if kind == "freelance" and not low.startswith("seeking freelancer"):
                continue
            if include and not any(k in low for k in include):
                continue
            if include_re and not include_re.search(post["text"]):
                continue
            if any(k in low for k in exclude):
                continue
            if job.get("require_remote") and not post["remote"]:
                continue
            if not post["email"] and not post["website"]:
                continue
            dom = domain_of(post["website"], post["email"])
            if db.add_lead(conn, company=post["company"], website=post["website"], domain=dom,
                           email=post["email"], email_source="post" if post["email"] else "",
                           segment=job["segment"], source=f"hn_{kind}",
                           source_text=f"Hacker News '{story.get('title')}' post:\n{post['text'][:3000]}"):
                added += 1
    return added


# --------------------------------------------------------------------------- OpenStreetMap
OVERPASS = "https://overpass-api.de/api/interpreter"


def overpass_query(bbox: list[float], tags: list[str]) -> str:
    s, w, n, e = bbox
    parts = []
    for tag in tags:
        k, v = tag.split("=", 1)
        for site_key in ("website", "contact:website", "url"):
            parts.append(f'nwr["{k}"="{v}"]["{site_key}"]({s},{w},{n},{e});')
    return f"[out:json][timeout:120];({''.join(parts)});out tags center;"


def parse_osm(elements: list[dict]) -> list[dict]:
    out = []
    for el in elements:
        t = el.get("tags", {})
        site = t.get("website") or t.get("contact:website") or t.get("url") or ""
        if not t.get("name") or not site:
            continue
        email = (t.get("email") or t.get("contact:email") or "").split(";")[0].strip().lower()
        out.append({"company": t["name"], "website": site, "email": email,
                    "city": t.get("addr:city", ""), "desc": "; ".join(
                        f"{k}={v}" for k, v in t.items() if k in (
                            "office", "shop", "description", "opening_hours", "addr:street", "operator"))})
    return out


def run_osm(job: dict) -> int:
    added = 0
    with db.connect() as conn:
        for city in job["cities"]:
            if added >= job.get("max_new", 30):
                break
            bbox = config.settings()["cities"][city]
            r = requests.post(OVERPASS, data={"data": overpass_query(bbox, job["tags"])}, headers=UA, timeout=180)
            r.raise_for_status()
            for biz in parse_osm(r.json().get("elements", [])):
                if added >= job.get("max_new", 30):
                    break
                dom = domain_of(biz["website"])
                if any(s in dom for s in SKIP_DOMAINS + ("facebook.com", "instagram.com")):
                    continue
                if db.add_lead(conn, company=biz["company"], website=biz["website"], domain=dom,
                               email=biz["email"] or None, email_source="osm" if biz["email"] else "",
                               city=city, country=job.get("country", ""), segment=job["segment"],
                               source="osm", source_text=f"OpenStreetMap listing in {city}: {biz['desc']}"):
                    added += 1
            time.sleep(5)  # Overpass fair-use
    return added


RUNNERS = {"yc": run_yc, "hn": run_hn, "osm": run_osm}


def run(only: str | None = None) -> dict:
    results = {}
    for job in config.settings().get("prospecting", []):
        if not job.get("enabled", True) or (only and job["source"] != only):
            continue
        name = f"{job['source']}->{job['segment']}"
        try:
            results[name] = RUNNERS[job["source"]](job)
        except (requests.RequestException, ValueError, KeyError) as e:
            results[name] = f"error: {e}"
        print(f"  {name}: {results[name]}")
    return results
