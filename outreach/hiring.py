"""Companies that just said, in public, that they need someone. Free sources, each a `prospecting` job:

  ats        Open roles on startups' own job boards (Greenhouse, Lever, Ashby public APIs). Checks the
             startups already in your leads (YC, Launch HN, funding news...) plus any you list, and finds
             fresh intern / AI-engineer / contract postings. The post becomes the email's hook.
  funding    Funding news RSS (Inc42, Entrackr, YourStory, TechCrunch...). A startup that raised money
             in the last two weeks is about to hire: "congrats on the round" is a true, timely opening.
  github     Startups' public repos with open "good first issue" tickets (GitHub search API). Fix one,
             then email: "I opened a PR fixing X" is the strongest internship opener there is.
  search     Directory pages found with Firecrawl search (e.g. HubSpot partner agencies, Bayut agency
             pages). Uses Firecrawl credits, within the daily cap.

Only fresh postings count: every source has a max age, and a post without a date is skipped.
"""
from __future__ import annotations

import html
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests

from . import config, db, firecrawl, website
from .importer import domain_of
from .prospect import UA, _clean
from .sources import _age_days, _email_in, _parse_time, job_matches, rejects_ai_application


def _get(url: str, **kw):
    return requests.get(url, headers=kw.pop("headers", UA), timeout=kw.pop("timeout", 30), **kw)


def _state(key: str) -> str:
    with db.connect() as conn:
        return db.get_state(conn, key) or ""


def _set_state(key: str, value) -> None:
    with db.connect() as conn:
        db.set_state(conn, key, str(value))


# =========================================================================== company job boards (ATS)
def _greenhouse(slug: str) -> list[dict] | None:
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return [{"board": "Greenhouse", "id": j.get("id"), "title": j.get("title", ""),
             "location": (j.get("location") or {}).get("name", ""), "job_type": "",
             "tags": " ".join(d.get("name", "") for d in j.get("departments") or []),
             "text": _clean(html.unescape(j.get("content") or "")), "apply": j.get("absolute_url", ""),
             "posted": _parse_time(j.get("first_published") or j.get("updated_at"))}
            for j in r.json().get("jobs", [])]


def _lever(slug: str) -> list[dict] | None:
    r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return None
    return [{"board": "Lever", "id": j.get("id"), "title": j.get("text", ""),
             "location": (j.get("categories") or {}).get("location", "") + " " + (j.get("workplaceType") or ""),
             "job_type": (j.get("categories") or {}).get("commitment", ""),
             "tags": (j.get("categories") or {}).get("team", ""),
             "text": j.get("descriptionPlain") or _clean(j.get("description") or ""), "apply": j.get("hostedUrl", ""),
             "posted": _parse_time((j.get("createdAt") or 0) / 1000 if j.get("createdAt") else None)}
            for j in data]


def _ashby(slug: str) -> list[dict] | None:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return [{"board": "Ashby", "id": j.get("id"), "title": j.get("title", ""),
             "location": f"{j.get('location', '')} {'remote' if j.get('isRemote') else ''}".strip(),
             "job_type": j.get("employmentType", ""), "tags": j.get("department", "") or "",
             "text": j.get("descriptionPlain") or _clean(j.get("descriptionHtml") or ""), "apply": j.get("jobUrl", ""),
             "posted": _parse_time(j.get("publishedAt"))}
            for j in r.json().get("jobs", []) if j.get("isListed", True)]


ATS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


@lru_cache(maxsize=None)
def ats_jobs(provider: str, slug: str) -> tuple:
    """One fetch per board per run, however many jobs in settings.yaml read it. () when no board."""
    try:
        return tuple(ATS[provider](slug) or ())
    except (requests.RequestException, ValueError):
        return ()


def find_board(name: str, domain: str) -> str:
    """'greenhouse:acme' for the company's public job board, '' if none of the three has one.
    Remembered for 30 days, so each company costs a few requests once a month."""
    key = f"ats:{domain or name.lower()}"
    cached = _state(key)
    if cached:
        found, _, checked = cached.partition("|")
        if found != "none" or checked > (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat():
            return "" if found == "none" else found
    candidates = list(dict.fromkeys(([domain.split(".")[0]] if domain else []) + website.slugs(name)))
    for slug in candidates[:3]:
        for provider, fetch in ATS.items():
            try:
                jobs = fetch(slug)
            except (requests.RequestException, ValueError):
                continue
            if jobs is not None:
                _set_state(key, f"{provider}:{slug}|")
                return f"{provider}:{slug}"
        time.sleep(0.3)
    _set_state(key, f"none|{datetime.now(timezone.utc).date().isoformat()}")
    return ""


def _ats_companies(job: dict) -> list[tuple[str, str, str]]:
    """(name, domain, 'provider:slug' or '') for the companies to check this run, oldest-checked first."""
    out = [(c.get("name", c.get("board", "")), c.get("domain", ""), c.get("board", ""))
           for c in job.get("companies", []) if isinstance(c, dict)]
    out += [("", "", b) for b in job.get("boards_list", [])]
    sources = tuple(job.get("from_sources", ["yc", "launch_hn", "funding", "github"]))
    marks = ",".join("?" * len(sources))
    with db.connect() as conn:
        rows = conn.execute(f"SELECT company, domain FROM leads WHERE source IN ({marks}) AND domain != '' "
                            "AND status NOT IN ('unsubscribed','bounced','rejected') ORDER BY id DESC LIMIT 400",
                            sources).fetchall()
    out += [(r["company"] or "", r["domain"], "") for r in rows]
    cursor = int(_state(f"ats:cursor:{job['segment']}") or 0)   # rotate through the list across runs
    n = job.get("max_checks", 40)
    picked = out[cursor:cursor + n] or out[:n]
    _set_state(f"ats:cursor:{job['segment']}", cursor + n if cursor + n < len(out) else 0)
    return picked


def run_ats(job: dict) -> int:
    added = 0
    for name, domain, board in _ats_companies(job):
        if added >= job.get("max_new", 10):
            break
        board = board or find_board(name, domain)
        if not board:
            continue
        provider, _, slug = board.partition(":")
        for post in ats_jobs(provider, slug):
            post = {**post, "company": name or slug}
            if not post["posted"] or not job_matches(post, {"max_age_days": 7, **job}):
                continue
            key = f"ats:{provider}:{slug}:{post['id']}"
            if _state(key):
                continue
            _set_state(key, 1)
            site = f"https://{domain}" if domain else website.find(name or slug, post["text"])
            text = (f"Open role on their own job board ({post['board']}, posted {post['posted'].date()}): "
                    f"{post['title']} | {post['job_type'] or 'type n/a'} | {post['location'] or 'location n/a'}\n"
                    f"{post['text'][:2500]}\nApply: {post['apply']}")
            if _hiring_lead(name or slug, site, text, f"Hiring: {post['title']}", job, "ats", _email_in(post["text"])):
                added += 1
            break  # one role per company is enough for the hook
    return added


def _hiring_lead(company: str, site: str, text: str, note: str, job: dict, source: str, email: str = "") -> bool:
    """Add a new lead, or give a known one a fresh hook (and re-research it) when it hasn't been emailed."""
    dom = domain_of(site, email)
    with db.connect() as conn:
        known = conn.execute("SELECT * FROM leads WHERE domain=? AND domain != ''", (dom,)).fetchone() if dom else None
        if known:
            if known["status"] in ("new", "enriched", "verified", "researched", "unfit", "invalid"):
                db.set_lead(conn, known["id"], source_text=(text + "\n\n" + (known["source_text"] or ""))[:4000],
                            notes=note[:200], segment=job["segment"],
                            status="verified" if known["status"] in ("researched", "unfit") else known["status"])
                return True
            return False
        return db.add_lead(conn, company=company[:80], website=site, domain=dom, email=email or None,
                           email_source="post" if email else "", segment=job["segment"], source=source,
                           source_text=text[:4000], notes=note[:200])


# =========================================================================== funding news
FUNDING_FEEDS = {
    "inc42": "https://inc42.com/buzz/feed/",
    "entrackr": "https://entrackr.com/feed/",
    "yourstory": "https://yourstory.com/feed",
    "techcrunch": "https://techcrunch.com/category/startups/feed/",
}
RAISE = re.compile(r"^(?P<who>.{2,90}?)\s+(?:raises|raised|secures|bags|nets|lands|closes|gets|picks up|scores)\s+"
                   r"(?P<what>(?=.*?(?:\$|₹|rs\.?\s|inr|usd|million|mn\b|crore|cr\b|seed|series|pre-series|funding|round)).{1,80})",
                   re.I)
# "Bengaluru-based AI startup Sarvam" -> "Sarvam"; "Fintech platform Jar" -> "Jar"
DESCRIPTOR = re.compile(r"^.*\b(?:startup|start-up|platform|company|firm|maker|marketplace|provider|app|brand|"
                        r"unicorn|player|venture|-based)\s+", re.I)


def parse_raise(title: str) -> dict | None:
    m = RAISE.search(html.unescape(title or "").strip())
    if not m:
        return None
    who = re.sub(r"^(exclusive|breaking|funding alert)\s*[:|-]\s*", "", m.group("who"), flags=re.I)
    company = DESCRIPTOR.sub("", who).strip(" '\"‘’“”:-")
    if not company or len(company.split()) > 5 or company.lower() in ("it", "startup", "company"):
        return None
    return {"company": company, "round": m.group("what").strip()}


def parse_feed(xml_text: str) -> list[dict]:
    out = []
    for item in ET.fromstring(xml_text).iter("item"):
        out.append({"title": item.findtext("title") or "", "link": item.findtext("link") or "",
                    "text": _clean(item.findtext("description") or ""),
                    "posted": _parse_time(item.findtext("pubDate"))})
    return out


def run_funding(job: dict) -> int:
    added = 0
    for name in job.get("feeds", ["inc42", "entrackr"]):
        url = FUNDING_FEEDS.get(name, name)
        try:
            items = parse_feed(_get(url, headers={"User-Agent": website.UA}).text)
        except (requests.RequestException, ET.ParseError) as e:
            print(f"    {name}: {e}")
            continue
        for item in items:
            if added >= job.get("max_new", 8):
                return added
            cap = (config.settings().get("freshness") or {}).get("news_days", 14)
            if not item["posted"] or _age_days(item["posted"]) > min(job.get("max_age_days", 14), cap):
                continue
            deal = parse_raise(item["title"])
            if not deal:
                continue
            blob = f"{item['title']} {item['text']}"
            if job.get("include_regex") and not re.search(job["include_regex"], blob, re.I):
                continue
            key = f"funding:{deal['company'].lower()}"
            if _state(key):
                continue
            _set_state(key, item["posted"].date().isoformat())
            site = website.find(deal["company"], "", job.get("tlds", ["com", "ai", "io", "in", "co"]), hint="startup")
            if not site:
                continue
            text = (f"Funding news ({item['posted'].date()}): {item['title']}\n{item['text'][:1500]}\n{item['link']}")
            if _hiring_lead(deal["company"], site, text, f"Just raised: {deal['round']}"[:200], job, "funding"):
                added += 1
    return added


# =========================================================================== GitHub good first issues
GH = "https://api.github.com"


def _gh(path: str, **params):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA["User-Agent"]}
    if os.getenv("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.getenv('GITHUB_TOKEN')}"
    r = requests.get(GH + path, params=params, headers=headers, timeout=30)
    if r.status_code in (403, 429):
        raise ValueError("GitHub rate limit reached (add a free GITHUB_TOKEN to .env for more)")
    r.raise_for_status()
    return r.json()


def run_github(job: dict) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=job.get("max_age_days", 14))).date().isoformat()
    added = checks = 0
    for topic in job.get("topics", ["llm", "ai-agents", "automation"]):
        q = (f"topic:{topic} good-first-issues:>1 stars:{job.get('min_stars', 30)}..{job.get('max_stars', 8000)} "
             f"pushed:>{since} archived:false")
        repos = _gh("/search/repositories", q=q, sort="updated", order="desc", per_page=20).get("items", [])
        for repo in repos:
            if added >= job.get("max_new", 6) or checks >= job.get("max_checks", 15):
                return added
            owner = repo.get("owner") or {}
            if owner.get("type") != "Organization" or _state(f"github:{repo['full_name']}"):
                continue
            checks += 1
            _set_state(f"github:{repo['full_name']}", 1)
            org = _gh(f"/orgs/{owner['login']}")
            site = org.get("blog") or repo.get("homepage") or ""
            if site and "://" not in site:
                site = "https://" + site
            if not site or "github.io" in site:
                continue
            issues = [i for i in _gh(f"/repos/{repo['full_name']}/issues", labels="good first issue", state="open",
                                     sort="created", direction="desc", per_page=5)
                      if "pull_request" not in i and _age_days(_parse_time(i.get("created_at"))) <= 60]
            if not issues or rejects_ai_application(repo.get("description") or ""):
                continue
            text = (f"Open-source repo {repo['full_name']} ({repo.get('stargazers_count')} stars): "
                    f"{repo.get('description') or ''}\nOpen 'good first issue' tickets (newest first):\n"
                    + "\n".join(f"- #{i['number']} {i['title']} ({i['html_url']}, opened {i['created_at'][:10]})"
                                for i in issues[:3])
                    + "\nHook idea: he fixes one of these and mentions the PR in the email.")
            if _hiring_lead(org.get("name") or owner["login"], site, text,
                            f"Good first issue: #{issues[0]['number']} {issues[0]['title']}"[:200], job, "github",
                            org.get("email") or ""):
                added += 1
            time.sleep(0.5)
    return added


# =========================================================================== directories via Firecrawl search
def run_search(job: dict) -> int:
    """Each Firecrawl result is a company page or a directory profile; the company's own site comes from
    the result itself, or from the profile page's links. Costs Firecrawl credits (daily cap applies)."""
    added = 0
    for query in job.get("queries", []):
        for hit in firecrawl.search(query, limit=job.get("per_query", 5)):
            if added >= job.get("max_new", 5):
                return added
            key = f"search:{hit['url']}"
            if _state(key):
                continue
            _set_state(key, 1)
            name = re.split(r"\s[|\-–—:]\s", hit["title"])[0].strip()[:80]
            directories = job.get("directories", [])
            site = "" if any(d in hit["url"] for d in directories) else website.from_text(hit["url"])
            if not site:  # a directory profile: the company's own site is one of the page's outbound links
                page = firecrawl.scrape(hit["url"]) or {}
                site = _outbound(page.get("markdown", "") + " " + page.get("html", ""), directories) \
                    or website.find(name, "")
            if not site or not name:
                continue
            text = f"Listed in {hit['url']}: {hit['title']}\n{hit['description']}"
            if _hiring_lead(name, site, text, f"Directory: {query}"[:200], job, "search"):
                added += 1
    return added


def _outbound(text: str, directories: list[str]) -> str:
    from urllib.parse import urlparse
    for url in re.findall(r"https?://[^\s<>\"')\]]+", text or ""):
        host = urlparse(url).netloc.lower()
        if host and not any(d in host for d in directories) and website.from_text(url):
            return f"https://{host}"
    return ""


RUNNERS = {"ats": run_ats, "funding": run_funding, "github": run_github, "search": run_search}
