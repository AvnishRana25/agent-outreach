"""Offline tests for the Firecrawl fallbacks: enrich, page finding, website search, credit cap."""
import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    from outreach import db
    db.init()


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.ok, self.text = status, data, status < 400, str(data)

    def json(self):
        return self._data


def fake_api(monkeypatch, handler):
    from outreach import firecrawl
    calls = []

    def post(url, json, headers, timeout):
        calls.append((url.rsplit("/", 1)[-1], json))
        assert headers["Authorization"] == "Bearer fc-test"
        return handler(url.rsplit("/", 1)[-1], json)
    monkeypatch.setattr(firecrawl.requests, "post", post)
    return calls


SHELL = "<html><head><title>Palm Homes</title></head><body><div id='root'></div><script src='app.js'></script></body></html>"
RENDERED = ("<html><head><title>Palm Homes</title></head><body><h1>Palm Homes Real Estate</h1>"
            "<a href='https://wa.me/9715000'>WhatsApp</a> Off-plan apartments in JVC and Dubai Marina. " + "x " * 200 +
            "</body></html>")


def test_enrich_uses_firecrawl_for_js_shell_and_map(monkeypatch):
    from outreach import enrich

    def handler(path, body):
        if path == "scrape":
            if body["url"].endswith("/get-in-touch"):
                return Resp(200, {"success": True, "data": {"rawHtml": "<p>Email sara@palmhomes.ae</p>" + "y " * 200,
                                                            "markdown": "Email sara@palmhomes.ae"}})
            return Resp(200, {"success": True, "data": {"rawHtml": RENDERED, "markdown": "Palm Homes Real Estate. "
                                                        "Off-plan apartments in JVC.", "metadata": {"title": "Palm Homes"}}})
        if path == "map":
            return Resp(200, {"success": True, "links": [{"url": "https://palmhomes.ae/get-in-touch"},
                                                         {"url": "https://palmhomes.ae/blog/2024/why-jvc"},
                                                         {"url": "https://other.com/contact"}]})
        raise AssertionError(path)
    calls = fake_api(monkeypatch, handler)
    monkeypatch.setattr(enrich, "_fetch", lambda url: SHELL if url == "https://palmhomes.ae" else "")
    sig, text = enrich.analyse("palmhomes.ae")
    assert sig["whatsapp_link"] and "sara@palmhomes.ae" in sig["emails_on_site"]
    assert "/get-in-touch" in sig["pages_found"] and "/blog/2024/why-jvc" not in sig["pages_found"]
    assert "Off-plan apartments" in text
    assert [c[0] for c in calls] == ["scrape", "map", "scrape"]


def test_readable_site_costs_no_credits(monkeypatch):
    from outreach import enrich, firecrawl
    calls = fake_api(monkeypatch, lambda path, body: Resp(200, {"success": True, "links": []}))
    page = RENDERED.replace("WhatsApp", "WhatsApp hello@palmhomes.ae")
    monkeypatch.setattr(enrich, "_fetch", lambda url: page)
    sig, _ = enrich.analyse("https://palmhomes.ae")
    assert sig["emails_on_site"] == ["hello@palmhomes.ae"] and calls == []
    assert firecrawl.used_today() == 0


def test_daily_cap_and_out_of_credits(monkeypatch):
    from outreach import config, firecrawl
    real = config.settings()
    monkeypatch.setattr(firecrawl.config, "settings", lambda: {**real, "firecrawl": {"daily_credit_cap": 2}})
    calls = fake_api(monkeypatch, lambda path, body: Resp(200, {"success": True, "data": {"markdown": "m"}}))
    assert firecrawl.scrape("https://a.com") and firecrawl.scrape("https://b.com")
    assert firecrawl.scrape("https://c.com") is None and len(calls) == 2      # capped

    monkeypatch.setattr(firecrawl.config, "settings", lambda: {**real, "firecrawl": {"daily_credit_cap": 100}})
    fake_api(monkeypatch, lambda path, body: Resp(402, {"error": "Payment required"}))
    assert firecrawl.scrape("https://d.com") is None
    assert firecrawl.used_today() >= 10**6                                    # no more tries today


def test_disabled_without_key(monkeypatch):
    from outreach import firecrawl
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    assert not firecrawl.enabled("scrape")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:3002")          # self-hosted needs no key
    assert firecrawl.enabled("scrape")


def test_website_search_fallback(monkeypatch):
    from outreach import website
    fake_api(monkeypatch, lambda path, body: Resp(200, {"success": True, "data": {"web": [
        {"url": "https://www.bayut.com/companies/desert-keys", "title": "Desert Keys on Bayut"},
        {"url": "https://desertkeys-realty.ae/about", "title": "Desert Keys Properties"}]}}))
    monkeypatch.setattr(website, "_resolves", lambda host: False)            # guessing fails
    monkeypatch.setattr(website, "_fetch", lambda url: "<title>Desert Keys Properties | Dubai</title>"
                        if url == "https://desertkeys-realty.ae" else "")
    assert website.resolve("Desert Keys Properties", ["ae"], hint="real estate Dubai") == "https://desertkeys-realty.ae"
