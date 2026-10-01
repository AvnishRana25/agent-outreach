"""More free prospect sources. Each runner takes one `prospecting` job from settings.yaml.

  jobs          Remote job boards with public feeds (Remotive, Himalayas, RemoteOK, Jobicy,
                We Work Remotely). Contract/part-time automation roles -> freelance leads;
                intern/junior roles -> internship leads.
  launch_hn     Hacker News "Launch HN" posts: YC startups that just launched.
  companies_house  UK Companies House API: active agencies by industry (SIC) code and town,
                with director names. Free API key.
  dld           Dubai Land Department register of brokerage offices (a CSV you download once).
  apify_maps    Google Maps listings through Apify's free monthly credit. Off by default: it
                sits in a grey area of Google's terms.

Paged sources remember where they stopped (prospect_state table), so each run sees new companies.
"""
from __future__ import annotations

import csv
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import requests

from . import config, db, website
from .importer import domain_of
from .prospect import EMAIL_RE, UA, _clean, deobfuscate

BROWSER_UA = website.UA
NO_AI_APPLICATION = re.compile(
    r"\b(?:no|without|don't use|do not use)[\s-]+(?:any\s+)?(?:ai|llm|chatgpt|gpt)[ -]?(?:generated|written|assisted)?[\s-]*"
    r"(?:applications?|emails?|cover letters?|responses?|proposals?)\b|"
    r"\b(?:ai|llm|chatgpt|gpt)[ -]?(?:generated|written|assisted)\s+"
    r"(?:applications?|emails?|cover letters?|responses?|proposals?)\s+"
    r"(?:will\s+not\s+be\s+reviewed|(?:will be|are)\s+(?:ignored|rejected|disqualified|not reviewed))\b",
    re.I,
)


def rejects_ai_application(text: str) -> bool:
    return bool(NO_AI_APPLICATION.search(text or ""))


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=kw.pop("headers", UA), timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


def _email_in(text: str) -> str:
    for e in EMAIL_RE.findall(deobfuscate(text or "")):
        e = e.lower().rstrip(".")
        if not e.endswith((".png", ".jpg", ".webp", ".svg", ".gif")) and "example." not in e:
            return e
    return ""


def _age_days(when: datetime | None) -> float:
    if not when:
        return 0.0
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def _parse_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    try:  # ISO 8601, including fractional seconds and "Z" (Ashby, Greenhouse, GitHub)
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(str(value).replace("Z", "+0000")[:31], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# =========================================================================== job boards
# Each board's feed is normalised to: board, id, title, company, location, job_type, text, apply, posted

def _remotive() -> list[dict]:
    out = []
    for j in _get("https://remotive.com/api/remote-jobs", params={"limit": 300}).json().get("jobs", []):
        out.append({"board": "Remotive", "id": j.get("id"), "title": j.get("title", ""),
                    "company": j.get("company_name", ""), "location": j.get("candidate_required_location", ""),
                    "job_type": j.get("job_type", ""), "tags": " ".join(j.get("tags") or []),
                    "text": _clean(j.get("description", "")), "apply": j.get("url", ""),
                    "posted": _parse_time(j.get("publication_date"))})
    return out


def _himalayas() -> list[dict]:
    out = []
    for offset in range(0, 200, 20):  # the feed pages 20 at a time
        jobs = _get("https://himalayas.app/jobs/api", params={"limit": 20, "offset": offset}).json().get("jobs", [])
        for j in jobs:
            out.append({"board": "Himalayas", "id": j.get("guid") or j.get("applicationLink"),
                        "title": j.get("title", ""), "company": j.get("companyName", ""),
                        "location": ", ".join(j.get("locationRestrictions") or []),
                        "job_type": j.get("employmentType", ""),
                        "tags": " ".join((j.get("categories") or []) + (j.get("seniority") or [])),
                        "text": _clean(j.get("description") or j.get("excerpt") or ""),
                        "apply": j.get("applicationLink", ""), "posted": _parse_time(j.get("pubDate"))})
        if len(jobs) < 20:
            break
        time.sleep(1)
    return out


def _remoteok() -> list[dict]:
    out = []
    for j in _get("https://remoteok.com/api", headers=BROWSER_UA).json():
        if not isinstance(j, dict) or not j.get("position"):
            continue  # the first element is a legal notice
        out.append({"board": "RemoteOK", "id": j.get("id"), "title": j.get("position", ""),
                    "company": j.get("company", ""), "location": j.get("location", ""),
                    "job_type": "", "tags": " ".join(j.get("tags") or []),
                    "text": _clean(j.get("description", "")), "apply": j.get("apply_url") or j.get("url", ""),
                    "posted": _parse_time(j.get("epoch") or j.get("date"))})
    return out


def _jobicy() -> list[dict]:
    out = []
    for j in _get("https://jobicy.com/api/v2/remote-jobs", params={"count": 100}).json().get("jobs", []):
        out.append({"board": "Jobicy", "id": j.get("id"), "title": j.get("jobTitle", ""),
                    "company": j.get("companyName", ""), "location": j.get("jobGeo", ""),
                    "job_type": " ".join(j.get("jobType") or []) if isinstance(j.get("jobType"), list)
                    else (j.get("jobType") or ""),
                    "tags": " ".join(j.get("jobIndustry") or []) + " " + (j.get("jobLevel") or ""),
                    "text": _clean(j.get("jobDescription") or j.get("jobExcerpt") or ""),
                    "apply": j.get("url", ""), "posted": _parse_time(j.get("pubDate"))})
    return out


WWR_FEEDS = ["https://weworkremotely.com/categories/remote-programming-jobs.rss",
             "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
             "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"]


def parse_wwr(xml_text: str) -> list[dict]:
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        title = item.findtext("title") or ""
        company, _, role = title.partition(":")
        out.append({"board": "We Work Remotely", "id": item.findtext("guid") or item.findtext("link"),
                    "title": role.strip() or title, "company": company.strip() if role else "",
                    "location": item.findtext("region") or "", "job_type": item.findtext("type") or "",
                    "tags": item.findtext("category") or "", "text": _clean(item.findtext("description") or ""),
                    "apply": item.findtext("link") or "", "posted": _parse_time(item.findtext("pubDate"))})
    return out


def _wwr() -> list[dict]:
    out = []
    for url in WWR_FEEDS:
        out += parse_wwr(_get(url, headers=BROWSER_UA).text)
        time.sleep(1)
    return out


def _workingnomads() -> list[dict]:
    out = []
    for j in _get("https://www.workingnomads.com/api/exposed_jobs/").json():
        out.append({"board": "Working Nomads", "id": j.get("url"), "title": j.get("title", ""),
                    "company": j.get("company_name", ""), "location": j.get("location", ""),
                    "job_type": "", "tags": f"{j.get('category_name', '')} {j.get('tags', '')}",
                    "text": _clean(j.get("description", "")), "apply": j.get("url", ""),
                    "posted": _parse_time((j.get("pub_date") or "")[:19])})
    return out


BOARDS = {"remotive": _remotive, "himalayas": _himalayas, "remoteok": _remoteok, "jobicy": _jobicy,
          "wwr": _wwr, "workingnomads": _workingnomads}


@lru_cache(maxsize=None)
def board_jobs(name: str) -> tuple:
    """Fetched once per run, however many jobs in settings.yaml read the same board."""
    return tuple(BOARDS[name]())


def job_matches(job: dict, rule: dict) -> bool:
    title = job["title"].lower()
    blob = f"{job['title']} {job['tags']} {job['job_type']} {job['text']}".lower()
    if rejects_ai_application(blob):
        return False
    if rule.get("title_regex") and not re.search(rule["title_regex"], job["title"], re.I):
        return False
    if rule.get("include_regex") and not re.search(rule["include_regex"], blob, re.I):
        return False
    if rule.get("job_types") and not any(t in f"{job['job_type']} {title}".lower() for t in rule["job_types"]):
        return False
    if any(x.lower() in blob for x in rule.get("exclude_any", [])):
        return False
    loc = (job["location"] or "").lower()
    allowed = [a.lower() for a in rule.get("allowed_locations", [])]
    if allowed and loc and not any(a in loc for a in allowed):
        return False
    cap = (config.settings().get("freshness") or {}).get("job_post_days", 7)
    if not job["posted"] or _age_days(job["posted"]) > min(rule.get("max_age_days", 7), cap):
        return False  # only fresh postings: an undated one could be months old
    return bool(job["company"])


def run_jobs(job: dict) -> int:
    added = checked = 0
    budget = job.get("max_checks", job.get("max_new", 10) * 4)  # website look-ups are slow; cap them
    for board in job.get("boards", list(BOARDS)):
        try:
            feed = board_jobs(board)
        except (requests.RequestException, ValueError, ET.ParseError) as e:
            print(f"    {board}: {e}")
            continue
        for post in feed:
            if added >= job.get("max_new", 10) or checked >= budget:
                return added
            if not job_matches(post, job):
                continue
            key = f"jobs:{board}:{post['id']}"
            with db.connect() as conn:
                state = db.get_state(conn, key)
                if state and (not state.startswith("retry:") or state[6:] > datetime.now(timezone.utc).date().isoformat()):
                    continue
            checked += 1
            email = _email_in(post["text"])
            site = website.find(post["company"], post["text"] + " " + post["apply"],
                                job.get("tlds", ["com", "io", "ai", "co", "dev", "app"]), hint="company")
            if not site and not email:
                with db.connect() as conn:
                    db.set_state(conn, key, "retry:" + (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat())
                continue
            posted = post["posted"].date().isoformat() if post["posted"] else "recently"
            text = (f"Remote job post on {post['board']} ({posted}): {post['title']} at {post['company']}"
                    f" | type: {post['job_type'] or 'n/a'} | location: {post['location'] or 'anywhere'}\n"
                    f"{post['text'][:2500]}\nApply: {post['apply']}")
            with db.connect() as conn:
                if db.add_lead(conn, company=post["company"][:80], website=site, domain=domain_of(site, email),
                               email=email or None, email_source="post" if email else "",
                               segment=job["segment"], source=f"jobs_{board}", source_text=text,
                               notes=f"Hiring: {post['title']}"[:200]):
                    added += 1
                db.set_state(conn, key, 1)
    return added


# =========================================================================== Launch HN
LAUNCH_RE = re.compile(r"^Launch HN:\s*(.+?)\s*\(YC ([A-Z]\d{2,4})\)\s*[–—:-]?\s*(.*)$", re.I)


def parse_launch(hit: dict) -> dict | None:
    m = LAUNCH_RE.match(hit.get("title") or "")
    if not m:
        return None
    text = _clean(hit.get("story_text") or "")
    site = hit.get("url") or website.from_text(hit.get("story_text") or "")
    return {"company": m.group(1).strip(), "batch": m.group(2).upper(), "pitch": m.group(3).strip(),
            "website": site, "email": _email_in(text), "text": text,
            "posted": _parse_time(hit.get("created_at_i")), "hn_id": hit.get("objectID")}


def run_launch_hn(job: dict) -> int:
    days = min(job.get("max_age_days", 30), (config.settings().get("freshness") or {}).get("news_days", 14) * 2)
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    hits = _get("https://hn.algolia.com/api/v1/search_by_date", params={
        "tags": "story", "query": "Launch HN", "hitsPerPage": 200,
        "numericFilters": f"created_at_i>{since}"}).json().get("hits", [])
    include = re.compile(job["include_regex"], re.I) if job.get("include_regex") else None
    added = 0
    with db.connect() as conn:
        for hit in hits:
            if added >= job.get("max_new", 10):
                break
            post = parse_launch(hit)
            if not post or not (post["website"] or post["email"]) or rejects_ai_application(post["text"]):
                continue
            if include and not include.search(f"{post['pitch']} {post['text']}"):
                continue
            text = (f"Launch HN ({post['posted'].date() if post['posted'] else ''}), YC {post['batch']}: "
                    f"{post['company']} - {post['pitch']}\n{post['text'][:2500]}\n"
                    f"https://news.ycombinator.com/item?id={post['hn_id']}")
            if db.add_lead(conn, company=post["company"], website=post["website"],
                           domain=domain_of(post["website"], post["email"]), email=post["email"] or None,
                           email_source="post" if post["email"] else "", segment=job["segment"],
                           source="launch_hn", source_text=text, notes=f"Launched on HN, YC {post['batch']}"):
                added += 1
    return added


# =========================================================================== UK Companies House
CH_API = "https://api.company-information.service.gov.uk"
SIC_LABELS = {"73110": "advertising agencies", "73120": "media representation", "70210": "public relations",
              "70229": "management consultancy", "62012": "business software development",
              "62020": "IT consultancy", "74100": "specialised design", "63120": "web portals"}


def _ch(path: str, **params) -> dict:
    key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    if not key:
        raise ValueError("COMPANIES_HOUSE_API_KEY is not set (free: developer.company-information.service.gov.uk)")
    for attempt in range(3):
        r = requests.get(CH_API + path, params=params, auth=(key, ""), timeout=30)
        if r.status_code == 429:  # 600 requests / 5 min
            if attempt < 2:
                time.sleep(60 * (attempt + 1))
            continue
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()
    raise requests.HTTPError("Companies House rate limit persisted after three retries")


def officer_name(raw: str) -> tuple[str, str]:
    """'SMITH, John Andrew' -> ('John', 'Smith')."""
    last, _, rest = (raw or "").partition(",")
    first = rest.strip().split(" ")[0] if rest.strip() else ""
    return first.title(), last.strip().title()


def active_directors(officers: dict) -> list[tuple[str, str]]:
    out = []
    for o in officers.get("items", []):
        role = o.get("officer_role") or ""
        if o.get("resigned_on") or role.startswith("corporate"):
            continue
        if "director" not in role and "member" not in role:
            continue
        first, last = officer_name(o.get("name", ""))
        if first:
            out.append((first, last))
    return out


def run_companies_house(job: dict) -> int:
    added = checked = 0
    budget = job.get("max_checks", job.get("max_new", 10) * 6)
    combos = [(loc, sic) for loc in job["locations"] for sic in job["sic_codes"]]
    with db.connect() as conn:
        start = int(db.get_state(conn, f"ch:{job['segment']}:combo", "0"))
    for i in range(len(combos)):
        loc, sic = combos[(start + i) % len(combos)]
        key = f"ch:{loc}:{sic}"
        with db.connect() as conn:
            cursor = db.get_state(conn, key, "0")
        if cursor == "done":
            continue
        params = {"sic_codes": sic, "location": loc, "company_status": "active", "size": 50,
                  "start_index": int(cursor), "incorporated_from": job.get("incorporated_from", "2012-01-01")}
        if job.get("incorporated_to"):
            params["incorporated_to"] = job["incorporated_to"]
        items = _ch("/advanced-search/companies", **params).get("items", [])
        for j, c in enumerate(items):
            if added >= job.get("max_new", 10) or checked >= budget:
                with db.connect() as conn:  # resume at this exact company next run
                    db.set_state(conn, key, int(cursor) + j)
                    db.set_state(conn, f"ch:{job['segment']}:combo", (start + i) % len(combos))
                return added
            if c.get("company_type") not in ("ltd", "llp", "private-limited-guarant-nsc", None):
                continue
            checked += 1
            num = c.get("company_number", "")
            profile = _ch(f"/company/{num}")
            if (profile.get("accounts", {}).get("last_accounts", {}).get("type") == "dormant"
                    or profile.get("has_insolvency_history")):
                continue
            directors = active_directors(_ch(f"/company/{num}/officers", items_per_page=20))
            if not directors or len(directors) > job.get("max_directors", 5):
                continue  # no named person, or too big to be a small agency
            name = c.get("company_name", "")
            site = website.resolve(name, job.get("tlds", ["co.uk", "com", "agency", "uk", "io"]),
                                   must_contain=num, hint="UK agency")
            if not site:
                continue
            town = (c.get("registered_office_address") or {}).get("locality", loc)
            sics = ", ".join(f"{s} ({SIC_LABELS.get(s, 'other')})" for s in c.get("sic_codes") or [sic])
            text = (f"UK Companies House: {name} (company no. {num}), incorporated {c.get('date_of_creation')}, "
                    f"registered in {town}. Industry codes: {sics}. "
                    f"Active directors: {', '.join(f'{f} {l}' for f, l in directors)}.")
            first, last = directors[0]
            with db.connect() as conn:
                if db.add_lead(conn, first_name=first, last_name=last, title="Director", company=name.title(),
                               website=site, domain=domain_of(site), country="United Kingdom", city=town,
                               segment=job["segment"], source="companies_house", source_text=text):
                    added += 1
            time.sleep(0.5)
        with db.connect() as conn:
            db.set_state(conn, key, "done" if len(items) < 50 else int(cursor) + len(items))
    return added


# =========================================================================== Dubai Land Department
DLD_COLUMNS = {
    "name": ["office_name_en", "name_en", "office name", "office_name", "company_name_en", "company name",
             "broker_office_name_en", "trade_name_en", "name"],
    "licence": ["license_number", "licence_number", "license no", "office_license_number", "trade_license",
                "license", "licence", "office_number", "real_estate_number"],
    "email": ["email", "office_email", "e-mail"],
    "phone": ["phone", "office_phone", "mobile", "telephone", "tel"],
    "website": ["website", "web_site", "webpage", "url"],
}


def dld_columns(header: list[str]) -> dict[str, str]:
    low = {h.strip().lower(): h for h in header if h}
    found = {}
    for field, names in DLD_COLUMNS.items():
        for n in names:
            match = next((orig for l, orig in low.items() if l == n), None) or \
                    next((orig for l, orig in low.items() if n in l and "_ar" not in l), None)
            if match:
                found[field] = match
                break
    return found


def run_dld(job: dict) -> int:
    path = Path(job.get("csv", "data/dld_offices.csv"))
    path = path if path.is_absolute() else config.ROOT / path
    if not path.exists():
        if not job.get("url"):
            raise ValueError(f"{path} not found. Download the DLD brokerage-offices CSV (see PLAYBOOK) to that path.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_get(job["url"], headers=BROWSER_UA, timeout=120).content)
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return 0
    cols = dld_columns(list(rows[0].keys()))
    if "name" not in cols:
        raise ValueError(f"Can't find the office-name column in {path.name}: {list(rows[0])[:12]}")
    with db.connect() as conn:
        cursor = int(db.get_state(conn, f"dld:{path.name}", "0"))
    added = checked = 0
    budget = job.get("max_checks", job.get("max_new", 10) * 5)
    i = cursor
    while i < len(rows) and added < job.get("max_new", 10) and checked < budget:
        row = rows[i]
        i += 1
        name = (row.get(cols["name"]) or "").strip()
        if not name:
            continue
        checked += 1
        licence = (row.get(cols.get("licence", ""), "") or "").strip()
        email = (row.get(cols.get("email", ""), "") or "").strip().lower()
        email = email if EMAIL_RE.fullmatch(email or "-") else ""
        site = (row.get(cols.get("website", ""), "") or "").strip()
        if site and "://" not in site:
            site = "https://" + site
        if not site:
            site = website.resolve(name, job.get("tlds", ["ae", "com"]), hint="real estate Dubai")
        if not site and not email:
            continue
        phone = (row.get(cols.get("phone", ""), "") or "").strip()
        text = (f"Dubai Land Department register of real-estate brokerage offices: {name}"
                f"{', licence ' + licence if licence else ''}{', phone ' + phone if phone else ''}.")
        with db.connect() as conn:
            if db.add_lead(conn, company=name.title(), website=site, domain=domain_of(site, email),
                           email=email or None, email_source="registry" if email else "", country="UAE",
                           city="Dubai", segment=job["segment"], source="dld", source_text=text):
                added += 1
    with db.connect() as conn:
        db.set_state(conn, f"dld:{path.name}", i)
    return added


# =========================================================================== Google Maps via Apify
APIFY_ACTOR = "compass~crawler-google-places"


def parse_places(items: list[dict], min_reviews: int, max_reviews: int) -> list[dict]:
    out = []
    for p in items:
        site = p.get("website") or ""
        reviews = p.get("reviewsCount") or 0
        if not p.get("title") or not site or not (min_reviews <= reviews <= max_reviews):
            continue
        if p.get("permanentlyClosed") or p.get("temporarilyClosed"):
            continue
        emails = p.get("emails") or []
        out.append({"company": p["title"], "website": site, "email": (emails[0] if emails else "").lower(),
                    "city": p.get("city") or "", "text": (
                        f"Google Maps listing: {p.get('categoryName') or 'business'}, rated "
                        f"{p.get('totalScore')} from {reviews} reviews, {p.get('address') or ''}.")})
    return out


def run_apify_maps(job: dict) -> int:
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        raise ValueError("APIFY_TOKEN is not set (free account at apify.com)")
    combos = [(q, loc) for loc in job["locations"] for q in job["queries"]]
    with db.connect() as conn:
        idx = int(db.get_state(conn, f"apify:{job['segment']}", "0"))
    query, loc = combos[idx % len(combos)]
    r = requests.post(f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
                      params={"token": token}, timeout=330,
                      json={"searchStringsArray": [query], "locationQuery": loc, "language": "en",
                            "maxCrawledPlacesPerSearch": job.get("max_places", 60), "skipClosedPlaces": True})
    r.raise_for_status()
    added = 0
    with db.connect() as conn:
        db.set_state(conn, f"apify:{job['segment']}", idx + 1)
        for p in parse_places(r.json(), job.get("min_reviews", 5), job.get("max_reviews", 2000)):
            if added >= job.get("max_new", 20):
                break
            if db.add_lead(conn, company=p["company"], website=p["website"], domain=domain_of(p["website"]),
                           email=p["email"] or None, email_source="maps" if p["email"] else "",
                           city=p["city"] or loc.split(",")[0], country=job.get("country", ""),
                           segment=job["segment"], source="apify_maps", source_text=p["text"]):
                added += 1
    return added


RUNNERS = {"jobs": run_jobs, "launch_hn": run_launch_hn, "companies_house": run_companies_house,
           "dld": run_dld, "apify_maps": run_apify_maps}


# =========================================================================== Meta Ad Library (by hand)
ADLIB_URL = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country={country}"
             "&q={q}&search_type=keyword_unordered&media_type=all")


def adlibrary_searches() -> list[dict]:
    from urllib.parse import quote
    cfg = config.settings().get("adlibrary", {})
    searches = [(c, k) for c in cfg.get("countries", ["AE", "IN"]) for k in cfg.get("keywords", ["real estate"])]
    day = datetime.now().timetuple().tm_yday
    todays = [searches[(day * 3 + i) % len(searches)] for i in range(min(3, len(searches)))]
    return [{"country": c, "keyword": k, "url": ADLIB_URL.format(country=c, q=quote(k))} for c, k in todays]


def adlibrary_tasks(path: Path) -> int:
    """Write today's Ad Library searches. Automated collection isn't allowed there, so you browse,
    and paste advertisers into data/adlibrary.csv for `import --source adlibrary`."""
    from urllib.parse import quote
    cfg = config.settings().get("adlibrary", {})
    searches = [(c, k) for c in cfg.get("countries", ["AE", "IN"]) for k in cfg.get("keywords", ["real estate"])]
    day = datetime.now().timetuple().tm_yday
    todays = [searches[(day * 3 + i) % len(searches)] for i in range(min(3, len(searches)))]
    lines = ["# Meta Ad Library: 15 minutes, by hand\n",
             "Open each search. Look for small brokerages running ads with a **Send WhatsApp message** "
             "button (click-to-WhatsApp). Those are the businesses with the exact problem your Realty Pandit "
             "work solves: ad leads landing in WhatsApp with no attribution or follow-up system.\n",
             "For each good advertiser (aim for 10), open their Page -> About for the website, then add a row to "
             "`data/adlibrary.csv` (header below). In `notes`, write what the ad offers and that it opens "
             "WhatsApp - the email will be built on it.\n",
             "```\ncompany,website,email,country,segment,notes\n"
             "Palm Homes,https://palmhomes.ae,,UAE,gulf_realestate,\"Click-to-WhatsApp ad for 1BR in JVC, "
             "running since Sep\"\n```\n",
             "Then: `python -m outreach import data/adlibrary.csv --source adlibrary` (duplicates are skipped, "
             "so re-importing the same file is fine).\n", "## Today's searches"]
    for country, kw in todays:
        lines.append(f"- {country} / \"{kw}\": {ADLIB_URL.format(country=country, q=quote(kw))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return len(todays)
