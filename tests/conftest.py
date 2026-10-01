import pytest

@pytest.fixture(autouse=True)
def isolate_test_credentials(monkeypatch):
    """Ensure external API keys from local .env do not leak into unit tests."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
