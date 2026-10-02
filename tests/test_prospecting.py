"""Offline tests for the B2B email prospecting pipeline.

Covers:
- Domain normalization & resolution (ignoring social media/directory platforms)
- Email syntax, role address, and disposable domain detection
- MX and catch-all validation (mocked DNS)
- Email pattern deduction and prioritized candidate generation
- Confidence scoring and evidence provenance
- Fallback provider credit capping and sequential fallback
- Bulk CSV parsing, deduplication, and daily target cap
- Persistence in SQLite and dashboard API endpoints
"""
import json
from unittest import mock
import pytest

from outreach import db
from outreach.prospecting.models import ProspectInput, ProspectResult
from outreach.prospecting.domain.normalizer import normalize_domain, is_ignored_domain
from outreach.prospecting.domain.resolver import resolve_domain
from outreach.prospecting.validation.syntax import is_valid_syntax, is_role_email, is_disposable_domain
from outreach.prospecting.validation.mx import check_mx
from outreach.prospecting.validation.catch_all import check_catch_all
from outreach.prospecting.email.patterns import deduce_pattern_from_email, detect_company_pattern
from outreach.prospecting.email.generator import generate_candidates
from outreach.prospecting.email.scorer import score_candidate, classify_confidence
from outreach.prospecting.pipeline.processor import enrich_prospect
from outreach.prospecting.pipeline.bulk import BulkProcessor, load_prospects_from_csv
from outreach.prospecting.providers import query_fallback_providers


def test_prospeo_uses_current_verified_email_api(monkeypatch):
    from outreach.prospecting.providers.prospeo import ProspeoProvider
    from outreach.prospecting.providers import budget
    monkeypatch.setenv("PROSPEO_API_KEY", "test")
    response = mock.Mock(status_code=200)
    response.json.return_value = {"error": False, "person": {"email": {
        "email": "Jane@acme.com", "status": "VERIFIED", "revealed": True}}}
    with mock.patch("outreach.prospecting.providers.prospeo.requests.post", return_value=response) as post:
        result = ProspeoProvider().find_email("Jane", "Doe", "Acme", "acme.com")
    assert result.email == "jane@acme.com" and result.status == "verified"
    assert post.call_args.args[0].endswith("/enrich-person")
    assert post.call_args.kwargs["json"] == {"only_verified_email": True, "data": {
        "first_name": "Jane", "last_name": "Doe", "company_website": "acme.com", "company_name": "Acme"}}
    response.status_code = 400
    response.json.return_value = {"error": True, "error_code": "NO_MATCH"}
    with mock.patch("outreach.prospecting.providers.prospeo.requests.post", return_value=response):
        assert ProspeoProvider().find_email("Jane", "Doe", "Acme", "acme.com").status == "not_found"
    response.status_code = 200
    response.json.return_value = {"response": {"remaining_credits": 12}}
    with mock.patch("outreach.prospecting.providers.budget.requests.get", return_value=response) as get:
        assert budget._prospeo_balance("test") == 12
    assert get.call_args.args[0].endswith("/account-information")


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_prospecting.db"
    monkeypatch.setenv("OUTREACH_DB", str(db_path))
    db.init()
    yield


# --------------------------------------------------------------------------- 1. Domain
def test_normalize_domain():
    # Standard URLs
    assert normalize_domain("https://www.acme.com/about?ref=1") == "acme.com"
    assert normalize_domain("http://startup.io/") == "startup.io"
    assert normalize_domain("www.company.co.uk") == "company.co.uk"
    assert normalize_domain("blog.agency.ai/team") == "agency.ai"

    # Social media and directories should be filtered out to empty string
    assert normalize_domain("https://www.linkedin.com/company/acme") == ""
    assert normalize_domain("https://twitter.com/acme") == ""
    assert normalize_domain("https://www.facebook.com/acme") == ""
    assert normalize_domain("https://instagram.com/acme") == ""
    assert normalize_domain("https://yelp.com/biz/acme") == ""
    assert is_ignored_domain("linkedin.com") is True

    # Empty / invalid
    assert normalize_domain("") == ""
    assert normalize_domain(None) == ""


def test_resolve_domain():
    # If domain provided, normalizes it
    assert resolve_domain("Acme Inc", "https://www.acme.com/about") == "acme.com"

    # If domain missing, cached domain is returned
    with db.connect() as conn:
        db.set_prospect_domain_cache(conn, "cachedcompany.com", detected_pattern="first.last")

    with db.connect() as conn:
        cached = db.get_prospect_domain_cache(conn, "cachedcompany.com")
        assert cached is not None

    # Name-based heuristic when search yields no result (strips 'Tech' suffix)
    with mock.patch("outreach.prospecting.domain.resolver.resolve_domain_from_search", return_value=""):
        res = resolve_domain("Alpha Beta Tech", None)
        assert res == "alphabeta.com"


# --------------------------------------------------------------------------- 2. Validation
def test_syntax_validation():
    assert is_valid_syntax("alex.smith@acme.com") is True
    assert is_valid_syntax("user+tag@domain.co.uk") is True
    assert is_valid_syntax("first_last@startup.io") is True

    assert is_valid_syntax("invalid..syntax@domain.com") is False
    assert is_valid_syntax("@nodomain.com") is False
    assert is_valid_syntax("nouser@") is False
    assert is_valid_syntax("spaces in@email.com") is False
    assert is_valid_syntax("") is False


def test_role_email_detection():
    assert is_role_email("info@acme.com") is True
    assert is_role_email("support@acme.com") is True
    assert is_role_email("sales@acme.com") is True
    assert is_role_email("admin@acme.com") is True
    assert is_role_email("hello@acme.com") is True
    assert is_role_email("contact@acme.com") is True
    assert is_role_email("billing@acme.com") is True

    # Real decision makers are not role emails
    assert is_role_email("alex.smith@acme.com") is False
    assert is_role_email("priya@acme.com") is False
    assert is_role_email("jdoe@acme.com") is False


def test_disposable_domain_detection():
    assert is_disposable_domain("mailinator.com") is True
    assert is_disposable_domain("tempmail.com") is True
    assert is_disposable_domain("10minutemail.com") is True

    assert is_disposable_domain("gmail.com") is False
    assert is_disposable_domain("acme.com") is False
    assert is_disposable_domain("stripe.com") is False


def test_mx_check_mocked():
    with mock.patch("dns.resolver.resolve") as mock_resolve:
        mock_mx1 = mock.MagicMock()
        mock_mx1.exchange = "aspmx.l.google.com."
        mock_resolve.return_value = [mock_mx1]

        has_mx, hosts, provider = check_mx("testmockdomain123.com")
        assert has_mx is True
        assert "aspmx.l.google.com" in hosts
        assert provider == "Google Workspace"


def test_catch_all_check():
    # Google Workspace / Microsoft 365 by default heuristic
    is_catch_all, method = check_catch_all("acme.com", ["aspmx.l.google.com"])
    assert is_catch_all is False
    assert method == "inferred_provider_default"


# --------------------------------------------------------------------------- 3. Patterns & Generator
def test_pattern_deduction():
    # Deduction from employee email localpart and names
    pat = deduce_pattern_from_email("priya.verma", ("Priya", "Verma"))
    assert pat == "first.last"

    pat = deduce_pattern_from_email("pverma", ("Priya", "Verma"))
    assert pat == "flast"

    pat = deduce_pattern_from_email("p.verma", ("Priya", "Verma"))
    assert pat == "f.last"

    pat = deduce_pattern_from_email("priyav", ("Priya", "Verma"))
    assert pat == "firstl"

    pat = deduce_pattern_from_email("priya", ("Priya", "Verma"))
    assert pat == "first"

    pat = deduce_pattern_from_email("verma", ("Priya", "Verma"))
    assert pat == "last"


def test_dominant_pattern_detection():
    # Multiple employee emails confirming first.last
    emails = [
        "alex.smith@acme.com",
        "jane.doe@acme.com",
        "robert.johnson@acme.com",
        "m.brown@acme.com",
    ]
    res = detect_company_pattern(emails, "acme.com")
    assert res["dominant_pattern"] == "first.last"
    assert res["match_count"] == 3


def test_candidate_generation():
    candidates = generate_candidates(
        first_name="Alex",
        last_name="Smith",
        domain="acme.com",
        preferred_pattern="first.last"
    )
    assert len(candidates) >= 10
    # Dominant pattern should be first
    assert candidates[0].email == "alex.smith@acme.com"
    assert candidates[0].pattern == "first.last"

    emails = [c.email for c in candidates]
    assert "alex@acme.com" in emails
    assert "asmith@acme.com" in emails
    assert "a.smith@acme.com" in emails
    assert "alexsmith@acme.com" in emails
    assert "smith.alex@acme.com" in emails


# --------------------------------------------------------------------------- 4. Scoring
def test_scoring_weights():
    cand = generate_candidates("Alex", "Smith", "acme.com", "first.last")[0]
    scored = score_candidate(
        candidate=cand,
        exact_public_match=True,
        pattern_match=True,
        multiple_employees=True,
        valid_mx=True,
        name_identity_match=True,
        is_catch_all=False,
        has_conflicting_patterns=False,
        is_role=False,
        public_url="https://acme.com/team"
    )
    # 45 (exact) + 25 (confirmed pattern) + 10 (multiple employees) + 10 (valid mx) + 10 (name match) = 100
    assert scored.score == 100
    conf_level, status = classify_confidence(scored.score)
    assert conf_level == "verified"
    assert status == "verified"

    # Catch-all penalty (-15) -> 85
    cand2 = generate_candidates("Alex", "Smith", "acme.com", "first.last")[0]
    scored_ca = score_candidate(
        candidate=cand2,
        exact_public_match=True,
        pattern_match=True,
        multiple_employees=True,
        valid_mx=True,
        name_identity_match=True,
        is_catch_all=True,
        has_conflicting_patterns=False,
        is_role=False
    )
    assert scored_ca.score == 85
    assert any(e.type == "catch_all_penalty" for e in scored_ca.evidence)


def test_unverified_candidate_scoring():
    # Unverified permutation with no public evidence
    cand = generate_candidates("Alex", "Smith", "acme.com")[0]
    scored = score_candidate(
        candidate=cand,
        exact_public_match=False,
        pattern_match=False,
        multiple_employees=False,
        valid_mx=True,
        name_identity_match=True,
        is_catch_all=False,
        has_conflicting_patterns=False,
        is_role=False
    )
    # Valid MX (+10), name identity (+10), unverified penalty (-20) -> clamped at 0
    assert scored.score == 0
    conf_level, status = classify_confidence(scored.score)
    assert conf_level == "unresolved"
    assert status == "not_found"


# --------------------------------------------------------------------------- 5. Fallback Providers
def test_fallback_dispatcher_skips_when_no_keys(monkeypatch):
    monkeypatch.delenv("PROSPEO_API_KEY", raising=False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.delenv("SKRAPP_API_KEY", raising=False)

    res = query_fallback_providers("Alex", "Smith", "Acme", "acme.com")
    assert res is None


# --------------------------------------------------------------------------- 6. Bulk Processing & CSV
def test_load_prospects_from_csv(tmp_path):
    csv_file = tmp_path / "prospects.csv"
    csv_file.write_text(
        "first_name,last_name,company,domain,title,linkedin_url\n"
        "Alex,Smith,Acme,acme.com,Founder,https://linkedin.com/in/alexsmith\n"
        "Jane,Doe,StartupX,startupx.ai,CEO,\n"
        ",,Missing Corp,missing.com,,\n"  # Missing name, should be ignored
    )

    loaded = load_prospects_from_csv(csv_file)
    assert len(loaded) == 2
    assert loaded[0].first_name == "Alex"
    assert loaded[0].company == "Acme"
    assert loaded[0].domain == "acme.com"
    assert loaded[1].first_name == "Jane"


def test_bulk_processor_deduplication():
    processor = BulkProcessor(max_workers=2, rate_limit_delay=0.0, target_verified=10)
    items = [
        ProspectInput(first_name="Alex", last_name="Smith", company="Acme", domain="acme.com", linkedin_url="https://linkedin.com/in/alex"),
        ProspectInput(first_name="Alex", last_name="Smith", company="Acme", domain="acme.com", linkedin_url="https://linkedin.com/in/alex"), # Duplicate
        ProspectInput(first_name="Jane", last_name="Doe", company="Beta", domain="beta.io"),
    ]

    with mock.patch.object(processor.processor, "process") as mock_proc:
        mock_proc.return_value = ProspectResult(
            first_name="Test", last_name="User", full_name="Test User",
            company="Test", domain="test.com", final_email="test@test.com",
            confidence_score=95, confidence_level="verified", email_status="verified"
        )
        results = processor.process_batch(items)
        assert len(results) == 2  # Deduplicated from 3 to 2


def test_bulk_processor_target_cap():
    processor = BulkProcessor(max_workers=1, rate_limit_delay=0.0, target_verified=2)
    items = [
        ProspectInput(first_name=f"User{i}", last_name="Test", company=f"Company{i}", domain=f"dom{i}.com")
        for i in range(5)
    ]

    with mock.patch.object(processor.processor, "process") as mock_proc:
        mock_proc.side_effect = [
            ProspectResult(first_name=f"User{i}", last_name="Test", full_name=f"User{i} Test", company=f"Company{i}",
                           domain=f"dom{i}.com", final_email=f"user{i}@dom{i}.com",
                           confidence_score=95, confidence_level="verified", email_status="verified")
            for i in range(5)
        ]
        results = processor.process_batch(items)
        # Should stop once 2 verified prospects are obtained
        assert len(results) == 2


def test_bulk_export_to_csv(tmp_path):
    processor = BulkProcessor()
    results = [
        ProspectResult(
            id=1, first_name="Alex", last_name="Smith", full_name="Alex Smith",
            company="Acme", domain="acme.com", final_email="alex.smith@acme.com",
            confidence_score=92, confidence_level="verified", email_status="verified",
            email_pattern="{first}.{last}@{domain}", source="local_evidence"
        )
    ]
    out_csv = tmp_path / "out.csv"
    processor.export_to_csv(results, out_csv)
    assert out_csv.exists()
    content = out_csv.read_text()
    assert "alex.smith@acme.com" in content
    assert "Alex Smith" in content


# --------------------------------------------------------------------------- 7. Processor End-to-End
def test_single_prospect_processor():
    input_data = ProspectInput(
        first_name="Devin",
        last_name="Vance",
        company="TechCorp",
        domain="techcorp.io",
        title="CTO"
    )

    with mock.patch("outreach.prospecting.pipeline.processor.check_mx") as mock_mx, \
         mock.patch("outreach.prospecting.pipeline.processor.discover_emails_from_site") as mock_site, \
         mock.patch("outreach.prospecting.pipeline.processor.search_exact_candidate_email") as mock_exact:

        mock_mx.return_value = (True, ["mail.techcorp.io"], "Generic")
        mock_site.return_value = {
            "employee_emails": ["sarah.connor@techcorp.io"],
            "role_emails": [],
            "all_emails": ["sarah.connor@techcorp.io"],
            "sources": {"sarah.connor@techcorp.io": "https://techcorp.io/team"}
        }
        mock_exact.return_value = (True, "https://techcorp.io/press")

        res = enrich_prospect(input_data)
        assert res.final_email == "devin.vance@techcorp.io"
        assert res.confidence_score >= 90
        assert res.email_status == "verified"
        assert res.id is not None

        # Verify saved in database
        with db.connect() as conn:
            saved = db.get_prospect(conn, res.id)
            assert saved is not None
            assert saved["final_email"] == "devin.vance@techcorp.io"
            assert saved["confidence_score"] >= 90

            stats = db.get_prospecting_stats(conn)
            assert stats["total"] >= 1
            assert stats["verified_today"] >= 1


# --------------------------------------------------------------------------- 8. Dashboard API Integration
def test_dashboard_prospects_api(tmp_path, monkeypatch):
    import http.client
    import threading
    from outreach import dashboard_local

    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpass1234")
    monkeypatch.setenv("SESSION_SECRET", "k" * 32)
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)

    srv = dashboard_local.server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port

    def call_api(method, path, body=None, cookie="", header=True):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json", "Host": "localhost"}
        if header:
            headers["X-Requested-With"] = "dashboard"
        if cookie:
            headers["Cookie"] = cookie
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        data = r.read()
        try:
            data = json.loads(data)
        except Exception:
            data = data.decode()
        return r.status, data, r.getheader("Set-Cookie") or ""

    try:
        # Login
        st, _, cookie_header = call_api("POST", "/api/login", {"password": "testpass1234"})
        assert st == 200
        cookie = cookie_header.split(";")[0]

        # GET /api/prospects/stats
        st, stats, _ = call_api("GET", "/api/prospects/stats", cookie=cookie)
        assert st == 200
        assert "target_daily" in stats
        assert stats["target_daily"] == 28

        # POST /api/prospects/enrich
        with mock.patch("outreach.prospecting.pipeline.processor.check_mx") as mock_mx, \
             mock.patch("outreach.prospecting.pipeline.processor.discover_emails_from_site") as mock_site, \
             mock.patch("outreach.prospecting.pipeline.processor.search_exact_candidate_email") as mock_exact:
            mock_mx.return_value = (True, ["mx.acme.com"], "Google Workspace")
            mock_site.return_value = {
                "employee_emails": ["marcus.aurelius@stoiccapital.com"],
                "role_emails": [],
                "all_emails": ["marcus.aurelius@stoiccapital.com"],
                "sources": {"marcus.aurelius@stoiccapital.com": "https://stoiccapital.com/about"}
            }
            mock_exact.return_value = (True, "https://stoiccapital.com/about")

            st, result, _ = call_api("POST", "/api/prospects/enrich", {
                "first_name": "Marcus",
                "last_name": "Aurelius",
                "company": "Stoic Capital",
                "domain": "stoiccapital.com",
                "title": "Partner"
            }, cookie=cookie)
            assert st == 200
            assert result["company"] == "Stoic Capital"
            assert result["final_email"] == "marcus.aurelius@stoiccapital.com"

        # GET /api/prospects
        st, data, _ = call_api("GET", "/api/prospects", cookie=cookie)
        assert st == 200
        assert data["total"] >= 1
        assert len(data["prospects"]) >= 1

        # GET /api/prospects/export
        st, csv_content, _ = call_api("GET", "/api/prospects/export", cookie=cookie)
        assert st == 200
        assert "Marcus Aurelius" in csv_content
        assert "marcus.aurelius@stoiccapital.com" in csv_content

    finally:
        srv.shutdown()
