import pytest

@pytest.fixture(autouse=True)
def isolate_test_credentials(monkeypatch):
    """Ensure external API keys from local .env do not leak into unit tests."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_provider_login(monkeypatch):
    """Tests stub transport.send; the pre-send login check would otherwise reach Zoho."""
    from outreach import transport
    monkeypatch.setattr(transport, "ready", lambda box: None)


@pytest.fixture(autouse=True)
def _no_web_search(monkeypatch):
    """The prospecting desk searches DuckDuckGo; tests must not depend on the network."""
    from outreach.prospecting.discovery import search
    from outreach.prospecting.domain import resolver
    monkeypatch.setattr(search, "_ddg_search", lambda query, limit=5: [])
    monkeypatch.setattr(resolver, "resolve_domain_from_search", lambda company: "")
