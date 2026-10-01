"""Offline tests for the extra prospect sources and the community digest (no network)."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import db
    db.init()
    yield


def _leads():
    from outreach import db
    with db.connect() as conn:
        return conn.execute("SELECT * FROM leads ORDER BY id").fetchall()


# --------------------------------------------------------------------------- website finder
def test_website_finder(monkeypatch):
    from outreach import website
    assert website.from_text("Apply at https://jobs.lever.co/acme or see https://acme.ai/about.") == "https://acme.ai"
    assert website.slugs("Bright Pixel Media Ltd") == ["brightpixelmedia", "bright-pixel-media", "brightpixel"]
    assert website.slugs("Palm Real Estate Brokers L.L.C")[0] == "palm"

    pages = {"https://brightpixelmedia.co.uk": "<title>Bright Pixel Media</title> Company no. 12345678",
             "https://brightpixelmedia.com": "<title>Domain for sale</title>"}
    monkeypatch.setattr(website, "_resolves", lambda host: f"https://{host}" in pages)
    monkeypatch.setattr(website, "_fetch", lambda url: pages.get(url, ""))
    # .com resolves but is parked; .co.uk names the company number -> chosen
    assert website.guess("Bright Pixel Media Ltd", ["com", "co.uk"], must_contain="12345678") == \
        "https://brightpixelmedia.co.uk"
    assert website.guess("Nobody Here Ltd", ["com"]) == ""
    assert not website.page_matches("<title>Bright Pixel Media</title>", "Bright Pixel Media Ltd", "12345678")


def test_search_site_checks_live_page_not_search_snippet(monkeypatch):
    from outreach import firecrawl, website
    monkeypatch.setattr(firecrawl, "search", lambda *a, **kw: [{"url": "https://brightpixel.co.uk",
                    "title": "Bright Pixel Media Ltd 12345678", "description": "Company website"}])
    monkeypatch.setattr(website, "_fetch", lambda url: "<title>Unrelated company</title>")
    assert website.search_site("Bright Pixel Media Ltd", must_contain="12345678") == ""


# --------------------------------------------------------------------------- job boards
def _job(**kw):
    base = {"board": "Remotive", "id": 1, "title": "Automation Engineer (Contract)", "company": "Acme",
            "location": "Worldwide", "job_type": "contract", "tags": "n8n python",
            "text": "Build n8n + WhatsApp flows. Email jobs [at] acme [dot] io", "apply": "https://acme.io/jobs",
            "posted": datetime.now(timezone.utc) - timedelta(days=2)}
    base.update(kw)
    return base


FREELANCE_RULE = {"job_types": ["contract", "freelance", "part-time"], "include_regex": r"n8n|whatsapp",
                  "exclude_any": ["us citizens"], "allowed_locations": ["worldwide", "india"], "max_age_days": 21}
INTERN_RULE = {"title_regex": r"\b(intern|junior)\b", "allowed_locations": ["worldwide", "india"]}


def test_job_routing():
    from outreach.sources import job_matches
    assert job_matches(_job(), FREELANCE_RULE)
    assert not job_matches(_job(), INTERN_RULE)
    assert job_matches(_job(title="AI Engineering Intern", job_type="internship"), INTERN_RULE)
    assert not job_matches(_job(location="USA only"), FREELANCE_RULE)
    assert not job_matches(_job(text="US citizens only. n8n"), FREELANCE_RULE)
    assert not job_matches(_job(posted=datetime.now(timezone.utc) - timedelta(days=40)), FREELANCE_RULE)
    assert not job_matches(_job(job_type="full-time", title="Automation Engineer"), FREELANCE_RULE)


def test_wwr_rss_parse():
    from outreach.sources import parse_wwr
    rss = """<rss><channel><item><title>Acme AI: Junior Backend Engineer</title>
      <region>Anywhere in the World</region><type>Full-Time</type><category>Programming</category>
      <link>https://weworkremotely.com/remote-jobs/acme-ai-junior</link>
      <pubDate>Mon, 28 Sep 2026 10:00:00 +0000</pubDate><description>&lt;p&gt;Python + LLMs&lt;/p&gt;</description>
      </item></channel></rss>"""
    [j] = parse_wwr(rss)
    assert (j["company"], j["title"], j["location"]) == ("Acme AI", "Junior Backend Engineer", "Anywhere in the World")
    assert "Python + LLMs" in j["text"] and j["posted"].year == 2026


def test_run_jobs_adds_once(monkeypatch):
    from outreach import sources
    monkeypatch.setattr(sources, "board_jobs", lambda name: (_job(),) if name == "remotive" else ())
    monkeypatch.setattr(sources.website, "find", lambda name, text, tlds, hint="": "https://acme.io")
    job = {**FREELANCE_RULE, "segment": "intl_freelance_posts", "boards": ["remotive", "jobicy"], "max_new": 5}
    assert sources.run_jobs(job) == 1
    assert sources.run_jobs(job) == 0          # the same post is never processed twice
    [lead] = _leads()
    assert lead["email"] == "jobs@acme.io" and lead["domain"] == "acme.io"
    assert "Remote job post on Remotive" in lead["source_text"]


def test_job_failure_does_not_mark_post_processed(monkeypatch):
    from outreach import db, sources
    monkeypatch.setattr(sources, "board_jobs", lambda name: (_job(),))
    def broken(*a, **kw):
        raise RuntimeError("site lookup failed")
    monkeypatch.setattr(sources.website, "find", broken)
    job = {**FREELANCE_RULE, "segment": "intl_freelance_posts", "boards": ["remotive"]}
    with pytest.raises(RuntimeError):
        sources.run_jobs(job)
    with db.connect() as conn:
        assert not db.get_state(conn, "jobs:remotive:1")


def test_job_without_contact_is_retried_later(monkeypatch):
    from outreach import db, sources
    monkeypatch.setattr(sources, "board_jobs", lambda name: (_job(text="No contact address"),))
    monkeypatch.setattr(sources.website, "find", lambda *a, **kw: "")
    job = {**FREELANCE_RULE, "segment": "intl_freelance_posts", "boards": ["remotive"]}
    assert sources.run_jobs(job) == 0
    with db.connect() as conn:
        state = db.get_state(conn, "jobs:remotive:1")
        assert state.startswith("retry:")
        db.set_state(conn, "jobs:remotive:1", "retry:2000-01-01")
    assert sources.run_jobs(job) == 0
    with db.connect() as conn:
        assert db.get_state(conn, "jobs:remotive:1").startswith("retry:")


def test_no_ai_application_instruction_excludes_post():
    from outreach import sources
    assert sources.rejects_ai_application("No AI-generated applications, please.")
    assert sources.rejects_ai_application("AI-generated applications will not be reviewed.")
    assert sources.rejects_ai_application("strict no-AI-generated-applications filter")
    assert not sources.rejects_ai_application("We build AI tools for recruiting.")
    assert not sources.job_matches(_job(text="No AI-generated applications, please."), FREELANCE_RULE)


# --------------------------------------------------------------------------- Launch HN
def test_launch_hn_parse():
    from outreach.sources import parse_launch
    post = parse_launch({"title": "Launch HN: Tessel (YC W26) – AI agents for freight brokers",
                         "url": "https://tessel.ai", "story_text": "Hi HN! Reach us at founders@tessel.ai",
                         "created_at_i": 1790000000, "objectID": "123"})
    assert (post["company"], post["batch"], post["website"], post["email"]) == \
        ("Tessel", "W26", "https://tessel.ai", "founders@tessel.ai")
    assert parse_launch({"title": "Show HN: my weekend project"}) is None


# --------------------------------------------------------------------------- Companies House
def test_companies_house(monkeypatch):
    from outreach import db, sources
    assert sources.officer_name("SMITH, John Andrew") == ("John", "Smith")
    officers = {"items": [
        {"name": "OLD, Gone", "officer_role": "director", "resigned_on": "2020-01-01"},
        {"name": "SECRETARIAL LTD", "officer_role": "corporate-secretary"},
        {"name": "PATEL, Priya", "officer_role": "director"}]}
    assert sources.active_directors(officers) == [("Priya", "Patel")]

    companies = [{"company_name": f"AGENCY {i} LTD", "company_number": f"0000000{i}", "company_type": "ltd",
                  "date_of_creation": "2018-05-01", "sic_codes": ["73110"],
                  "registered_office_address": {"locality": "Leeds"}} for i in range(3)]
    calls = []

    def fake_ch(path, **params):
        calls.append((path, params))
        if path == "/advanced-search/companies":
            return {"items": companies[params["start_index"]:]}
        if path.endswith("/officers"):
            return officers
        return {"accounts": {"last_accounts": {"type": "micro-entity"}}}
    monkeypatch.setattr(sources, "_ch", fake_ch)
    monkeypatch.setattr(sources.website, "resolve",
                        lambda name, tlds, must_contain="", hint="": f"https://{name.split()[1]}.co.uk")
    job = {"segment": "uk_agencies", "locations": ["Leeds"], "sic_codes": ["73110"], "max_new": 2}
    assert sources.run_companies_house(job) == 2
    first = _leads()[0]
    assert (first["first_name"], first["last_name"], first["title"]) == ("Priya", "Patel", "Director")
    assert "company no. 00000000" in first["source_text"]
    with db.connect() as conn:
        assert db.get_state(conn, "ch:Leeds:73110") == "2"   # resumes at the third company
    assert sources.run_companies_house(job) == 1
    assert "incorporated_to" not in calls[0][1]


# --------------------------------------------------------------------------- Dubai register
def test_dld_csv(tmp_path, monkeypatch):
    from outreach import sources
    csv_path = tmp_path / "offices.csv"
    csv_path.write_text("OFFICE_NUMBER,OFFICE_NAME_AR,OFFICE_NAME_EN,PHONE,EMAIL,WEBPAGE\n"
                        "1001,x,PALM HOMES REAL ESTATE BROKERS,+9714000,info@palmhomes.ae,\n"
                        "1002,x,DESERT KEYS PROPERTIES,+9714001,,www.desertkeys.ae\n"
                        "1003,x,NO TRACE BROKERS,+9714002,,\n")
    cols = sources.dld_columns(["OFFICE_NUMBER", "OFFICE_NAME_AR", "OFFICE_NAME_EN", "PHONE", "EMAIL", "WEBPAGE"])
    assert cols["name"] == "OFFICE_NAME_EN" and cols["licence"] == "OFFICE_NUMBER"
    monkeypatch.setattr(sources.website, "resolve", lambda *a, **k: "")
    job = {"segment": "gulf_realestate", "csv": str(csv_path), "max_new": 10}
    assert sources.run_dld(job) == 2
    a, b = _leads()
    assert a["email"] == "info@palmhomes.ae" and a["email_source"] == "registry"
    assert b["website"] == "https://www.desertkeys.ae" and "licence 1002" in b["source_text"]
    assert sources.run_dld(job) == 0           # cursor is at the end of the file


# --------------------------------------------------------------------------- Apify Google Maps
def test_parse_places():
    from outreach.sources import parse_places
    items = [{"title": "Sky Realty", "website": "https://sky.ae", "reviewsCount": 40, "totalScore": 4.6,
              "categoryName": "Real estate agency", "city": "Dubai", "emails": ["Hello@sky.ae"]},
             {"title": "Tiny", "website": "https://tiny.ae", "reviewsCount": 1},
             {"title": "Huge", "website": "https://huge.ae", "reviewsCount": 9000},
             {"title": "No site", "reviewsCount": 50}]
    [p] = parse_places(items, 5, 2000)
    assert p["email"] == "hello@sky.ae" and "4.6 from 40 reviews" in p["text"]


# --------------------------------------------------------------------------- community + ad library
REDDIT_ATOM = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_abc123</id><title>[Hiring] n8n + WhatsApp lead bot for my clinic</title>
<link href="https://www.reddit.com/r/forhire/comments/abc123/x/"/><author><name>/u/drsmith</name></author>
<content type="html">&lt;p&gt;Budget $300. Need WhatsApp replies into Google Sheets.&lt;/p&gt;</content>
<published>{now}</published></entry>
<entry><id>t3_def456</id><title>[For Hire] I build websites</title>
<link href="https://www.reddit.com/r/forhire/comments/def456/y/"/><content type="html">hi</content>
<published>{now}</published></entry></feed>"""


def test_community_digest(tmp_path, monkeypatch):
    from outreach import community, config
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    posts = community.parse_reddit_atom(REDDIT_ATOM.format(now=now))
    assert posts[0]["ext_id"] == "abc123" and posts[0]["author"] == "drsmith"
    assert "Budget $300" in posts[0]["body"]

    monkeypatch.setattr(community, "FETCHERS", {"reddit": lambda src: [dict(p) for p in posts]})
    monkeypatch.setattr(community.config, "settings", lambda: {"community": [
        {"name": "r/forhire", "type": "reddit", "subreddit": "forhire", "title_regex": r"\[hiring\]",
         "include_regex": "whatsapp|n8n"}]})
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert community.run(use_mock=True) == 1
    assert community.run(use_mock=True) == 0   # already seen
    digest = (tmp_path / "opportunities_today.md").read_text()
    assert "[Hiring] n8n + WhatsApp lead bot" in digest and "For Hire" not in digest


def test_adlibrary(tmp_path, monkeypatch):
    from outreach import importer, sources
    real = sources.config.settings()
    monkeypatch.setattr(sources.config, "settings", lambda: {**real, "adlibrary": {
        "countries": ["AE"], "keywords": ["real estate", "off plan"]}})
    assert sources.adlibrary_tasks(tmp_path / "a.md") == 2
    assert "country=AE&q=off%20plan" in (tmp_path / "a.md").read_text()
    csv_path = tmp_path / "adlibrary.csv"
    csv_path.write_text("company,website,country,segment,notes\n"
                        "Palm Homes,https://palmhomes.ae,UAE,gulf_realestate,CTWA ad for 1BR in JVC\n")
    assert importer.import_csv(csv_path, None, "adlibrary") == (1, 0)
    assert "Meta Ad Library running active ads: CTWA ad for 1BR" in _leads()[0]["source_text"]
