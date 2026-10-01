"""Guard rails for Prospeo / Hunter / Skrapp credits."""
import threading
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    for k in ("PROSPEO_API_KEY", "HUNTER_API_KEY", "SKRAPP_API_KEY"):
        monkeypatch.setenv(k, "test-key")
    from outreach import db
    from outreach.prospecting.providers import budget
    db.init()
    budget.new_run()
    monkeypatch.setattr(budget.time, "sleep", lambda s: None)
    # mid-month, so the daily cap is a predictable share of the month
    monkeypatch.setattr(budget, "_now", lambda: datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc))


class FakeProvider:
    def __init__(self, name="prospeo", results=None):
        self.name, self.calls, self.results = name, [], list(results or [])

    def is_available(self):
        return True

    def find_email(self, first, last, company, domain, prospect_id=None):
        from outreach.prospecting.models import ProviderResult
        self.calls.append((first, last, domain))
        status = self.results.pop(0) if self.results else "found"
        if status == "found":
            return ProviderResult(provider=self.name, email=f"{first}@{domain}".lower(), confidence=93, status="verified")
        return ProviderResult(provider=self.name, status=status)


def test_never_pays_twice_for_the_same_person():
    from outreach.prospecting.providers import budget
    p = FakeProvider(results=["not_found"])
    assert budget.lookup(p, "Ana", "Ruiz", "Acme", "acme.com").status == "not_found"
    assert budget.lookup(p, "ana", "ruiz", "ACME", "www.acme.com").status == "remembered_not_found"
    q = FakeProvider()
    assert budget.lookup(q, "Bo", "Li", "Acme", "acme.com").email == "bo@acme.com"
    again = budget.lookup(q, "Bo", "Li", "Acme", "acme.com")
    assert again.status == "remembered" and again.email == "bo@acme.com" and len(q.calls) == 1


def test_daily_cap_spreads_the_month_and_keeps_20_percent(monkeypatch):
    from outreach.prospecting.providers import budget
    st = budget.status("hunter")
    assert st["usable_monthly"] == 20 and st["plan_monthly"] == 25          # 80% of Hunter's 25 free searches
    assert st["daily_cap"] == 2                                             # 20 left over 16 days, max 2 a day
    h = FakeProvider("hunter")
    got = [budget.lookup(h, f"P{i}", "X", "Co", f"co{i}.com").status for i in range(4)]
    assert got[:2] == ["verified", "verified"] and got[2].startswith("skipped: daily budget used")
    assert len(h.calls) == 2


def test_monthly_budget_and_per_run_cap(monkeypatch):
    from outreach.prospecting.providers import budget
    monkeypatch.setattr(budget, "_now", lambda: datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc))  # last day: no spreading
    monkeypatch.setattr(budget, "settings", lambda name: {"plan_monthly": 5, "use_pct": 80, "daily_max": 50,
                                                          "per_run_max": 3, "min_interval_s": 0})
    p = FakeProvider()
    statuses = [budget.lookup(p, f"P{i}", "X", "Co", f"co{i}.com").status for i in range(5)]
    assert statuses.count("verified") == 3 and statuses[3] == "skipped: per-run budget used"
    budget.new_run()
    statuses = [budget.lookup(p, f"Q{i}", "X", "Co", f"q{i}.com").status for i in range(3)]
    assert statuses[0] == "verified" and statuses[1].startswith("skipped: monthly budget used (4/4)")


def test_parallel_bulk_workers_cannot_overspend(monkeypatch):
    from outreach.prospecting.providers import budget
    monkeypatch.setattr(budget, "_now", lambda: datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc))  # last day: no spreading
    monkeypatch.setattr(budget, "settings", lambda name: {"plan_monthly": 10, "use_pct": 50, "daily_max": 50,
                                                          "per_run_max": 50, "min_interval_s": 0})
    p = FakeProvider()
    threads = [threading.Thread(target=budget.lookup, args=(p, f"P{i}", "X", "Co", f"c{i}.com")) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(p.calls) == 5 and budget.status("prospeo")["used_month"] == 5


@pytest.mark.parametrize("code,why", [(401, "API key was rejected"), (402, "out of credits"), (429, "rate-limited")])
def test_provider_errors_pause_it(code, why):
    from outreach.prospecting.providers import budget
    p = FakeProvider(results=[f"http_{code}"])
    budget.lookup(p, "A", "B", "Co", "co.com")
    assert why in budget.status("prospeo")["paused"]
    assert budget.lookup(p, "C", "D", "Co", "co2.com").status.startswith("skipped: paused")
    assert len(p.calls) == 1


def test_low_real_balance_stops_spending(monkeypatch):
    import json
    from outreach import db
    from outreach.prospecting.providers import budget
    with db.connect() as conn:
        db.set_state(conn, "provider_balance:prospeo", json.dumps({"remaining": 10, "at": "2026-10-16T08:00:00+00:00"}))
    assert budget.lookup(FakeProvider(), "A", "B", "Co", "co.com").status.startswith("skipped: only 10 credits left")


def test_waterfall_stops_at_two_paid_calls(monkeypatch):
    from outreach.prospecting import providers
    a, b, c = FakeProvider("prospeo", ["not_found"]), FakeProvider("skrapp", ["not_found"]), FakeProvider("hunter")
    monkeypatch.setattr(providers, "get_providers", lambda: [a, b, c])
    assert providers.query_fallback_providers("A", "B", "Co", "co.com") is None
    assert len(a.calls) == 1 and len(b.calls) == 1 and not c.calls          # Hunter (scarcest) untouched


def test_hunter_key_is_never_in_the_url(monkeypatch):
    from outreach.prospecting.providers.hunter import HunterProvider
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"data": {"email": "ana@acme.com", "score": 95, "verification": {"status": "valid"}}}
    def get(url, params=None, timeout=None, headers=None):
        seen.update(params=params, headers=headers)
        return R()
    monkeypatch.setattr("outreach.prospecting.providers.hunter.requests.get", get)
    res = HunterProvider().find_email("Ana", "Ruiz", "Acme", "acme.com")
    assert res.email == "ana@acme.com" and res.status == "verified"
    assert "api_key" not in seen["params"] and seen["headers"]["X-API-KEY"] == "test-key"


def test_prospeo_answers_in_either_shape(monkeypatch):
    from outreach.prospecting.providers.prospeo import ProspeoProvider

    class R:
        def __init__(self, code, data):
            self.status_code, self.data = code, data
        def json(self):
            return self.data
    replies = [R(200, {"error": False, "response": {"email": {"email": "Ana@Acme.com", "email_status": "VALID"}}}),
               R(200, {"error": False, "response": {"email": "bo@acme.com", "email_status": "VERIFIED"}}),
               R(400, {"error": True, "message": "NO_RESULT"}), R(401, {"error": True})]
    monkeypatch.setattr("outreach.prospecting.providers.prospeo.requests.post", lambda *a, **k: replies.pop(0))
    p = ProspeoProvider()
    assert p.find_email("A", "R", "Acme", "acme.com").email == "ana@acme.com"
    assert p.find_email("B", "O", "Acme", "acme.com").status == "verified"
    assert p.find_email("C", "D", "Acme", "acme.com").status == "not_found"
    assert p.find_email("E", "F", "Acme", "acme.com").status == "http_401"


def _patched_pipeline(monkeypatch, catch_all=False):
    from outreach.prospecting.pipeline import processor
    monkeypatch.setattr(processor, "check_mx", lambda d: (True, ["mx.acme.com"], "Generic"))
    monkeypatch.setattr(processor, "check_catch_all", lambda d, mx: (catch_all, "test"))
    monkeypatch.setattr(processor, "discover_emails_from_site", lambda d: {"employee_emails": [], "sources": {}})
    monkeypatch.setattr(processor, "search_company_emails", lambda d: {})
    monkeypatch.setattr(processor, "search_name_with_domain", lambda n, d: (None, None))
    searches = []
    monkeypatch.setattr(processor, "search_exact_candidate_email", lambda e: searches.append(e) or (False, None))
    return processor, searches


def test_providers_are_skipped_when_they_cannot_help(monkeypatch):
    from outreach.prospecting import providers
    from outreach.prospecting.pipeline.processor import enrich_prospect
    calls = []
    monkeypatch.setattr(providers, "query_fallback_providers", lambda *a, **k: calls.append(a))
    _, searches = _patched_pipeline(monkeypatch, catch_all=True)
    res = enrich_prospect({"first_name": "Ana", "last_name": "Ruiz", "company": "Acme", "domain": "acme.com"})
    assert not calls and "accepts every address" in str(res.evidence)
    assert len(searches) == 3                                         # web searches capped per person
    enrich_prospect({"first_name": "Bo", "last_name": "", "company": "Acme", "domain": "acme.com"})
    assert not calls


def test_one_provider_hit_teaches_the_company_pattern(monkeypatch):
    from outreach.prospecting.models import ProviderResult
    from outreach.prospecting.pipeline.processor import enrich_prospect
    processor, searches = _patched_pipeline(monkeypatch)
    calls = []
    monkeypatch.setattr(processor, "query_fallback_providers", lambda first, last, company, domain: calls.append(first) or
                        ProviderResult(provider="prospeo", email=f"{first[0]}{last}@acme.com".lower(), confidence=93,
                                       status="verified"))
    first = enrich_prospect({"first_name": "Ana", "last_name": "Ruiz", "company": "Acme", "domain": "acme.com"})
    assert first.final_email == "aruiz@acme.com" and first.confidence_level == "verified"
    second = enrich_prospect({"first_name": "Bo", "last_name": "Chen", "company": "Acme", "domain": "acme.com"})
    assert second.final_email == "bchen@acme.com" and calls == ["Ana"]      # learned flast: no second paid call
