"""New lead sources (company job boards, funding news, GitHub, directory search) and the freshness rules,
against saved sample responses (no network)."""
import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    from outreach import db, hiring
    db.init()
    hiring.ats_jobs.cache_clear()


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw))


class R:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text or json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise __import__("requests").HTTPError(str(self.status_code))


def test_funding_headlines_name_the_company():
    from outreach.hiring import parse_raise
    assert parse_raise("Bengaluru-based AI startup Sarvam raises $41 Mn in Series A")["company"] == "Sarvam"
    assert parse_raise("Fintech platform Jar bags Rs 50 Cr funding")["company"] == "Jar"
    assert parse_raise("Exclusive: Tessel secures $3M seed round")["company"] == "Tessel"
    assert parse_raise("Why AI agents will change hiring") is None


def test_company_board_role_becomes_a_hook_for_a_known_startup(monkeypatch):
    from outreach import db, hiring
    with db.connect() as conn:
        db.add_lead(conn, company="Tessel", website="https://tessel.ai", domain="tessel.ai",
                    segment="intl_startups_intern", source="yc", status="researched", fit=5)
    fresh, old = _ago(days=2).isoformat(), _ago(days=40).isoformat()

    def get(url, **kw):
        if "greenhouse" in url and "/tessel/" in url:
            return R(data={"jobs": [
                {"id": 1, "title": "Senior Staff Engineer", "location": {"name": "Remote"}, "first_published": fresh},
                {"id": 2, "title": "AI Engineer Intern", "location": {"name": "Remote (India)"}, "first_published": old},
                {"id": 3, "title": "AI Engineer Intern", "location": {"name": "Remote (India)"},
                 "first_published": fresh, "content": "&lt;p&gt;Build LLM agents with us.&lt;/p&gt;",
                 "absolute_url": "https://boards.greenhouse.io/tessel/jobs/3"}]})
        return R(404, {})
    monkeypatch.setattr(hiring.requests, "get", get)
    job = {"source": "ats", "segment": "intl_startups_intern", "title_regex": r"\bintern", "max_checks": 5,
           "exclude_any": ["senior"], "allowed_locations": ["remote", "india"]}
    assert hiring.run_ats(job) == 1
    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE domain='tessel.ai'").fetchone()
    assert "AI Engineer Intern" in lead["source_text"] and "Build LLM agents" in lead["source_text"]
    assert lead["status"] == "verified"                       # re-researched with the new hook
    assert hiring.find_board("Tessel", "tessel.ai") == "greenhouse:tessel"
    hiring.ats_jobs.cache_clear()
    assert hiring.run_ats(job) == 0                            # the same posting isn't used twice


def test_lever_and_ashby_postings_are_read(monkeypatch):
    from outreach import hiring
    ms = int(_ago(days=1).timestamp() * 1000)
    monkeypatch.setattr(hiring.requests, "get", lambda url, **kw: R(data=[
        {"id": "a", "text": "Founding AI Engineer", "categories": {"commitment": "Contract", "location": "Remote"},
         "createdAt": ms, "hostedUrl": "https://jobs.lever.co/x/a", "descriptionPlain": "agents"}])
        if "lever" in url else R(data={"jobs": [{"id": "b", "title": "Applied AI Intern", "isRemote": True,
                                                 "publishedAt": _ago(days=1).isoformat(), "jobUrl": "u"}]}))
    lever, ashby = hiring.ats_jobs("lever", "x"), hiring.ats_jobs("ashby", "y")
    assert lever[0]["job_type"] == "Contract" and lever[0]["posted"].date() == _ago(days=1).date()
    assert ashby[0]["title"] == "Applied AI Intern" and "remote" in ashby[0]["location"]


def test_funding_news_adds_only_fresh_relevant_raises(monkeypatch):
    from outreach import db, hiring, website
    def item(title, days):
        return (f"<item><title>{title}</title><link>https://inc42.com/x</link><description>about</description>"
                f"<pubDate>{_ago(days=days).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>")
    feed = ("<rss><channel>" + item("AI startup Sarvam raises $41 Mn", 2) + item("Old Co raises $5 Mn seed", 30)
            + item("Bakery chain Crumbs raises Rs 10 Cr", 1) + "</channel></rss>")
    monkeypatch.setattr(hiring.requests, "get", lambda url, **kw: R(text=feed))
    monkeypatch.setattr(website, "find", lambda name, *a, **k: f"https://{name.split()[0].lower()}.ai")
    job = {"source": "funding", "segment": "india_startups_intern", "feeds": ["inc42"], "include_regex": r"\bai\b|saas"}
    assert hiring.run_funding(job) == 1
    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads").fetchone()
    assert lead["company"] == "Sarvam" and lead["source"] == "funding" and "raises $41 Mn" in lead["source_text"]


def test_github_good_first_issue_repos(monkeypatch):
    from outreach import db, hiring
    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/search/repositories"):
            assert "good-first-issues:>1" in params["q"]
            return R(data={"items": [{"full_name": "acme/agentkit", "owner": {"login": "acme", "type": "Organization"},
                                      "stargazers_count": 900, "description": "Agents toolkit", "homepage": ""},
                                     {"full_name": "bob/toy", "owner": {"login": "bob", "type": "User"}}]})
        if url.endswith("/orgs/acme"):
            return R(data={"name": "Acme AI", "blog": "acme.ai", "email": "team@acme.ai"})
        if "/issues" in url:
            return R(data=[{"number": 42, "title": "Add retry to the HTTP tool", "html_url": "https://github.com/acme/agentkit/issues/42",
                            "created_at": _ago(days=3).isoformat()}])
        raise AssertionError(url)
    monkeypatch.setattr(hiring.requests, "get", get)
    assert hiring.run_github({"source": "github", "segment": "intl_startups_intern", "topics": ["llm"]}) == 1
    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads").fetchone()
    assert lead["website"] == "https://acme.ai" and lead["email"] == "team@acme.ai"
    assert "#42 Add retry to the HTTP tool" in lead["source_text"]


def test_directory_search_finds_the_agency_site(monkeypatch):
    from outreach import db, firecrawl, hiring
    monkeypatch.setattr(firecrawl, "search", lambda q, limit=5: [
        {"url": "https://ecosystem.hubspot.com/marketplace/solutions/brightside", "title": "Brightside Digital | HubSpot Partner",
         "description": "Leeds agency"}])
    monkeypatch.setattr(firecrawl, "scrape", lambda url: {"markdown": "Visit https://ecosystem.hubspot.com/x or https://brightside.co.uk/contact", "html": ""})
    job = {"source": "search", "segment": "uk_agencies", "queries": ["q"], "directories": ["hubspot.com"]}
    assert hiring.run_search(job) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT company, website FROM leads").fetchone()[:] == ("Brightside Digital", "https://brightside.co.uk")


def test_undated_or_old_job_posts_are_skipped():
    from outreach.sources import job_matches
    post = {"title": "Automation contractor", "tags": "", "job_type": "contract", "text": "n8n", "location": "Remote",
            "company": "Acme"}
    assert job_matches({**post, "posted": _ago(days=2)}, {"max_age_days": 30})
    assert not job_matches({**post, "posted": _ago(days=10)}, {"max_age_days": 30})   # global cap: 7 days
    assert not job_matches({**post, "posted": None}, {})


def test_only_fresh_community_posts():
    from outreach.community import wanted
    src = {"max_age_hours": 72}
    assert wanted({"title": "x", "body": "", "posted": _ago(hours=5)}, src)
    assert not wanted({"title": "x", "body": "", "posted": _ago(hours=30)}, src)          # global cap: 24 h
    assert not wanted({"title": "x", "body": "", "posted": None}, src)


def test_new_sources_reach_an_existing_settings_file(tmp_path, monkeypatch):
    import shutil
    import yaml
    from outreach import config
    cfg = tmp_path / "config"
    shutil.copytree(config.CONFIG_DIR, cfg)
    example = yaml.safe_load((cfg / "settings.example.yaml").read_text())
    mine = {**example, "prospecting": [j for j in example["prospecting"] if j["source"] == "yc"],
            "community": [{"name": "r/forhire", "type": "reddit", "subreddit": "forhire", "max_age_hours": 48}]}
    (cfg / "settings.yaml").write_text(yaml.safe_dump(mine))
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    config.settings.cache_clear()
    try:
        s = config.settings()
        types = {j["source"] for j in s["prospecting"]}
        assert {"ats", "funding", "github", "yc"} <= types
        assert len([j for j in s["prospecting"] if j["source"] == "yc"]) == 2           # yours, untouched
        names = [c["name"] for c in s["community"]]
        assert names[0] == "r/forhire" and names.count("r/forhire") == 1 and "r/zapier hiring" in names
        assert s["freshness"]["community_hours"] == 24
    finally:
        config.settings.cache_clear()
