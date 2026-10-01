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
    from outreach.prospecting.pipeline import processor
    monkeypatch.setattr(processor, "search_github_domain_emails", lambda domain, limit=5: {})
    if hasattr(processor, "search_github_user_email"):
        monkeypatch.setattr(processor, "search_github_user_email", lambda name, domain: (None, None))


@pytest.fixture(autouse=True)
def isolate_test_config(monkeypatch):
    """Ensure local user settings.yaml does not leak into unit tests expecting default example settings."""
    from outreach import config
    orig_load = config._load_yaml

    def _test_load_yaml(name: str) -> dict:
        if name == "settings.yaml" and config.CONFIG_DIR == config.ROOT / "config":
            return orig_load("settings.example.yaml")
        return orig_load(name)

    monkeypatch.setattr(config, "_load_yaml", _test_load_yaml)
    if hasattr(config.settings, "cache_clear"):
        config.settings.cache_clear()
    if hasattr(config.profile, "cache_clear"):
        config.profile.cache_clear()
    yield
    if hasattr(config.settings, "cache_clear"):
        config.settings.cache_clear()
    if hasattr(config.profile, "cache_clear"):
        config.profile.cache_clear()


