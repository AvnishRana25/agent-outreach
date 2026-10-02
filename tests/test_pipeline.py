"""Offline tests: parsers, prompt rendering, LLM wrapper and the send/reply flow (no network)."""
from datetime import datetime, timezone
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    from outreach import db
    db.init()
    yield


def test_hn_post_parsing():
    from outreach.prospect import parse_hn_post
    post = parse_hn_post("Acme AI | Software Engineering Intern | REMOTE (global) | "
                         "<a href=\"https://acme.ai\">https://acme.ai</a><p>Email jobs [at] acme [dot] ai")
    assert post["company"] == "Acme AI"
    assert post["email"] == "jobs@acme.ai"
    assert post["website"] == "https://acme.ai"
    assert post["remote"]
    free = parse_hn_post("SEEKING FREELANCER | Bob's Bakery | need a CRM integration<p>bob at gmail dot com")
    assert free["company"] == "Bob's Bakery" and free["email"] == "bob@gmail.com"


def test_yc_filter():
    from outreach.prospect import yc_candidates
    data = [
        {"name": "A", "isHiring": True, "status": "Active", "team_size": 12, "regions": ["Remote", "United States of America"], "batch": "Summer 2024", "website": "https://a.com"},
        {"name": "B", "isHiring": True, "status": "Active", "team_size": 900, "regions": ["Remote"], "batch": "Winter 2023", "website": "https://b.com"},
        {"name": "C", "isHiring": False, "status": "Active", "team_size": 5, "regions": ["Remote"], "batch": "Winter 2024", "website": "https://c.com"},
        {"name": "D", "isHiring": True, "status": "Active", "team_size": 8, "regions": ["India"], "batch": "Winter 2024", "website": "https://d.com"},
    ]
    names = [c["name"] for c in yc_candidates(data, ["United States of America", "Remote"], [], 60, 2022)]
    assert names == ["A"]


def test_osm_parse_and_query():
    from outreach.prospect import overpass_query, parse_osm
    q = overpass_query([1, 2, 3, 4], ["office=estate_agent"])
    assert '"office"="estate_agent"' in q and "(1,2,3,4)" in q
    rows = parse_osm([{"tags": {"name": "Palm Realty", "website": "https://palmrealty.ae", "email": "info@palmrealty.ae", "office": "estate_agent"}},
                      {"tags": {"name": "No Site"}}])
    assert len(rows) == 1 and rows[0]["email"] == "info@palmrealty.ae"


def test_prompts_render():
    from outreach import config, personalize, replies, research
    for seg in config.settings()["segments"]:
        text = personalize.system_prompt(seg)
        assert "{" not in text.replace("{{", ""), seg
    assert "re_pipeline" in research._system()
    assert "booking link" in replies._system().lower()


def test_free_mail_not_deduped():
    from outreach import db
    with db.connect() as conn:
        assert db.add_lead(conn, email="a@gmail.com", domain="", segment="intl_freelance_posts")
        assert db.add_lead(conn, email="b@gmail.com", domain="", segment="intl_freelance_posts")
        assert db.add_lead(conn, email="x@acme.com", domain="acme.com", segment="uk_agencies")
        assert not db.add_lead(conn, email="y@acme.com", domain="acme.com", segment="uk_agencies")


def test_website_contact_stays_on_company_domain():
    from outreach.enrich import _best_email
    assert _best_email(["agency@gmail.com", "info@acme.com", "jane@acme.com"], "acme.com") == "jane@acme.com"
    assert _best_email(["agency@gmail.com"], "acme.com") == ""


def test_prepare_skips_crawl_when_send_queue_is_full(monkeypatch):
    import sys
    from outreach import cli, db, engine, prospect, sender
    monkeypatch.setattr(sys, "argv", ["outreach", "prepare"])
    monkeypatch.setattr(sender, "draft_room", lambda: {"room": 0, "waiting": 21, "capacity": 20})
    monkeypatch.setattr(prospect, "run", lambda **kw: pytest.fail("full queue should not trigger prospecting"))
    cli.main()
    with db.connect() as conn:
        assert "skipped discovery" in engine.job_state(conn, "prepare")["result"]


def test_prepare_uses_verified_backlog_before_crawling(monkeypatch):
    import sys
    from outreach import cli, db, enrich, personalize, prospect, research, sender, verify
    with db.connect() as conn:
        for i in range(9):
            db.add_lead(conn, email=f"person{i}@firm{i}.com", domain=f"firm{i}.com",
                        segment="uk_agencies", status="verified", email_status="valid", email_source="website")
    monkeypatch.setattr(sys, "argv", ["outreach", "prepare"])
    monkeypatch.setattr(sender, "draft_room", lambda: {"room": 3, "waiting": 0, "capacity": 20})
    monkeypatch.setattr(prospect, "run", lambda **kw: pytest.fail("verified backlog should be used first"))
    monkeypatch.setattr(enrich, "run", lambda *a: 0)
    monkeypatch.setattr(verify, "run", lambda: {})
    monkeypatch.setattr(verify, "confirm_guesses", lambda: 0)
    monkeypatch.setattr(research, "run", lambda *a: {"researched": 0})
    monkeypatch.setattr(personalize, "run", lambda *a: 0)
    cli.main()


def test_automatic_prospecting_skips_segments_not_allowed_to_send(monkeypatch):
    import json
    from outreach import db, prospect
    seen = []
    monkeypatch.setattr(prospect, "all_runners", lambda: {"sample": lambda job: seen.append(job["segment"]) or 1})
    monkeypatch.setattr(prospect.config, "settings", lambda: {"prospecting": [
        {"source": "sample", "segment": "off"}, {"source": "sample", "segment": "on"}]})
    assert prospect.run(allowed_segments=["on"]) == {"sample->on": 1}
    assert seen == ["on"]
    with db.connect() as conn:
        assert not db.get_state(conn, "source_health:sample->off")
        assert json.loads(db.get_state(conn, "source_health:sample->on"))["result"] == "1"


def test_llm_wrapper_parses_and_detects_daily_quota(monkeypatch):
    from google.genai import errors
    from outreach import llm, research
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_RPM", "100000")
    brief = research.Brief(company_summary="s", facts=[], pains=[], best_hook="h", proof_id="re_pipeline",
                           angle="a", contact_first_name="", contact_role="Founder", fit_score=8, fit_reason="r")
    fake = mock.MagicMock()
    fake.models.generate_content.return_value = mock.MagicMock(parsed=brief, text=brief.model_dump_json())
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(llm, "_skip", set())
    assert llm.generate("sys", "prompt", research.Brief).fit_score == 8
    fake.models.generate_content.side_effect = errors.ClientError(
        429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED",
                        "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}})
    with pytest.raises(llm.QuotaExhausted):
        llm.generate("sys", "prompt", research.Brief)


def test_india_share_cap():
    from outreach import db, personalize
    with db.connect() as conn:
        for i in range(30):
            db.add_lead(conn, email=f"o{i}@ind{i}.in", domain=f"ind{i}.in", segment="india_realestate", status="researched", fit=9, email_status="valid", email_source="website")
        for i in range(30):
            db.add_lead(conn, email=f"g{i}@gulf{i}.ae", domain=f"gulf{i}.ae", segment="gulf_realestate", status="researched", fit=8, email_status="valid", email_source="website")
        picked = personalize.pick_leads(conn, 28)
    india = sum(r["segment"] == "india_realestate" for r in picked)
    from outreach import config
    assert india <= int(28 * config.settings()["targeting"]["india_share_max"])
    assert len(picked) == 28


def test_full_flow_with_zoho_transport(monkeypatch):
    from outreach import db, personalize, research, review, sender, transport, replies
    settings = sender.config.settings()
    monkeypatch.setattr(sender.config, "settings", lambda: {**settings, "sending": {**settings["sending"], "allowed_segments": ["gulf_realestate"]}})
    with db.connect() as conn:
        db.add_lead(conn, email="omar@palmrealty.ae", domain="palmrealty.ae", company="Palm Realty",
                    first_name="Omar", segment="gulf_realestate", status="verified", email_status="valid",
                    email_source="website")
    research.run(10, use_mock=True)
    assert personalize.run(10, use_mock=True) == 1
    with db.connect() as conn:  # stand-in for a real Gemini draft: mock text is never sent
        omar_id = conn.execute("SELECT id FROM leads WHERE email='omar@palmrealty.ae'").fetchone()[0]
        conn.execute("UPDATE messages SET body='Hi Omar, here is a custom plan for Palm Realty.', review_note='', confidence=0.9 WHERE lead_id=?", (omar_id,))
        conn.execute("UPDATE leads SET research='{}' WHERE id=?", (omar_id,))
    assert review.bulk_approve(0) == 1

    sent = []
    def fake_send(box, to, subject, body, thread):
        sent.append((box["email"], to, subject, thread, body))
        return f"<m{len(sent)}@x>", f"z{len(sent)}"
    monkeypatch.setattr(transport, "send", fake_send)
    boxes = [{"email": "me@zoho.in", "transport": "zoho_api", "max_per_day": 35, "password": ""}]
    monkeypatch.setattr(sender.config, "inboxes", lambda: boxes)
    # the example profile still has github.com/YOUR-GITHUB, which the sender rightly refuses to send
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana\nIf this isn't relevant, reply \"no\" and I won't email again.")

    def at(*a):
        class F(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(*a, tzinfo=timezone.utc)
        return F
    with mock.patch.object(sender, "datetime", at(2026, 10, 14, 6, 0)):   # Wed 10:00 Dubai
        with db.connect() as conn:
            db.set_state(conn, "last_inbound_sync:me@zoho.in", "2026-10-14T06:00:00+00:00")
        assert sender.tick(5) == 1
    with mock.patch.object(sender, "datetime", at(2026, 10, 19, 6, 0)):   # Mon, day 5
        with db.connect() as conn:
            db.set_state(conn, "last_inbound_sync:me@zoho.in", "2026-10-19T06:00:00+00:00")
        assert sender.tick(5) == 1
    assert sent[1][3]["provider_id"] == "z1"  # follow-up replies in the same thread
    assert sent[1][2].startswith("Re: ")
    assert "reply \"no\" and I won't email again" in sent[0][4]

    # an unsubscribe reply cancels the rest and suppresses the address
    monkeypatch.setattr(transport, "fetch", lambda box, days, known=None: [transport.Incoming(
        message_id="<r1@x>", from_addr="omar@palmrealty.ae", from_header="Omar", subject="Re: x",
        body="please remove me", received_at="2026-10-19T08:00:00+00:00", refs="<m1@x>")])
    monkeypatch.setattr(replies, "classify", lambda lead, s, b: replies.ReplyClass(
        category="unsubscribe", summary="asked to stop", suggested_reply=""))
    monkeypatch.setattr(replies.config, "inboxes", lambda: boxes)
    assert replies.sync(4) == 1
    with db.connect() as conn:
        assert db.suppressed(conn, "omar@palmrealty.ae")
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE status='approved'").fetchone()[0] == 0


def test_new_lead_template_autoapproval_is_opt_in_capped_and_respects_no_ai(monkeypatch):
    from outreach import config, db, personalize
    settings = config.settings()
    assert settings["sending"]["auto_approve_daily_cap"] == 0
    monkeypatch.setattr(config, "settings", lambda: {**settings, "sending": {
        **settings["sending"], "auto_approve_daily_cap": 1,
        "auto_approve_segments": ["uk_agencies"], "allowed_segments": ["uk_agencies"]}})
    for i in range(3):
        number = f"12345{i}"
        site = f"Small agency {number} builds client websites."
        if i == 2:
            site += " No AI-generated applications will be reviewed."
        with db.connect() as conn:
            db.add_lead(conn, email=f"hello@agency{i}.co.uk", company=f"Agency {i}",
                        website=f"https://agency{i}.co.uk", segment="uk_agencies",
                        source="companies_house", source_text=f"UK Companies House: Agency {i} (company no. {number})",
                        site_text=site, status="researched", email_status="valid",
                        email_source="website", fit=8)
    monkeypatch.setattr(personalize, "generate", lambda row, angle=None: personalize.mock(row))
    monkeypatch.setattr(config, "placeholders", lambda: [])   # the example profile still says YOUR-GITHUB
    assert personalize.run(3) == 3
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads WHERE status='approved'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE step=0 AND status='draft'").fetchone()[0] == 2
        assert db.get_state(conn, f"auto_approved:{db.now()[:10]}") == "1"


def test_zoho_token_exchange_and_hints(monkeypatch, capsys):
    from outreach import cli, transport
    monkeypatch.setenv("ZOHO_CLIENT_ID", "1000.ID")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "sec")
    calls = []

    class R:
        def __init__(self, data): self.data = data
        def json(self): return self.data
    def post(url, data, timeout):
        calls.append((url, data))
        if data["grant_type"] == "authorization_code":
            return R({"refresh_token": "1000.refresh", "scope": "ZohoMail.messages.ALL ZohoMail.accounts.READ"})
        return R({"error": "invalid_code"})
    monkeypatch.setattr(transport.requests, "post", post)
    cli.zoho_token(" 1000.grant ")
    assert "ZOHO_REFRESH_TOKEN=1000.refresh" in capsys.readouterr().out
    assert calls[0][0] == "https://accounts.zoho.in/oauth/v2/token" and calls[0][1]["code"] == "1000.grant"
    box = {"email": "workwithavnish@zohomail.in", "zoho_dc": "in"}
    with pytest.raises(transport.ZohoError, match="zoho-token"):
        transport.zoho(box).token()


def test_mock_output_is_purged_and_never_sent(monkeypatch):
    from outreach import db, personalize, research, review, sender, transport
    with db.connect() as conn:
        db.add_lead(conn, email="omar@palmrealty.ae", domain="palmrealty.ae", company="Palm Realty",
                    first_name="Omar", segment="gulf_realestate", status="verified", email_status="valid")
    research.run(10, use_mock=True)
    personalize.run(10, use_mock=True)
    review.bulk_approve(0)                        # someone approves a placeholder by mistake
    sent = []
    monkeypatch.setattr(transport, "send", lambda *a: sent.append(a) or ("<m>", "z"))
    monkeypatch.setattr(sender.config, "inboxes", lambda: [{"email": "me@zoho.in", "transport": "zoho_api",
                                                              "max_per_day": 35, "password": ""}])
    sender.tick(5)
    assert sent == []
    with db.connect() as conn:
        lead = conn.execute("SELECT status, research, email_status FROM leads").fetchone()
        assert tuple(lead) == ("enriched", "", "unchecked")    # back in line for real research
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_email_cleaning_and_junk():
    from outreach import verify
    assert verify.clean("u003esupport@propsell.co") == "support@propsell.co"
    assert verify.clean("%20hello@eternalmedia.co.uk") == "hello@eternalmedia.co.uk"
    assert verify.check("info@example.com") == "invalid"
    assert verify.check("accounts@localfame.com") == "invalid"
    assert verify.check("noreply+x@acme.com") == "invalid"


def test_pause_switch(monkeypatch):
    from outreach import db, sender
    with db.connect() as conn:
        db.set_state(conn, "sending_paused", "1")
    assert sender.tick(5) == 0


def test_llm_falls_back_through_model_chain(monkeypatch):
    from google.genai import errors
    from outreach import llm, research
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_RPM", "100000")
    monkeypatch.setenv("GEMINI_RESEARCH_MODELS", "m-retired,m-small,m-big")
    monkeypatch.setattr(llm, "_skip", set())
    brief = research.Brief(company_summary="s", facts=[], pains=[], best_hook="h", proof_id="re_pipeline",
                           angle="a", contact_first_name="", contact_role="Founder", fit_score=8, fit_reason="r")
    tried = []
    quota = {"error": {"code": 429, "message": "Quota exceeded ... limit: 20, model: m-small",
                       "status": "RESOURCE_EXHAUSTED", "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}

    def gen(model, contents, config):
        tried.append(model)
        if model == "m-retired":
            raise errors.ClientError(404, {"error": {"code": 404, "message": "no longer available", "status": "NOT_FOUND"}})
        if model == "m-small":
            raise errors.ClientError(429, quota)
        return mock.MagicMock(parsed=brief)
    fake = mock.MagicMock()
    fake.models.generate_content.side_effect = gen
    monkeypatch.setattr(llm, "_client", fake)

    assert llm.generate("sys", "p", research.Brief, kind="research").fit_score == 8
    assert tried == ["m-retired", "m-small", "m-big"]
    tried.clear()
    llm.generate("sys", "p", research.Brief, kind="research")
    assert tried == ["m-big"]                                  # used-up models are skipped for the day
    st = llm.status()
    assert st["out"] == {"m-retired": "unavailable (404)", "m-small": "quota"}
    assert st["limits"] == {"m-small": 20} and st["calls"] == {"m-big": 2}

    monkeypatch.setattr(llm, "_skip", set())                  # a fresh process reads the shared state
    fake.models.generate_content.side_effect = lambda **k: (_ for _ in ()).throw(errors.ClientError(429, quota))
    with pytest.raises(llm.QuotaExhausted):
        llm.generate("sys", "p", research.Brief, kind="research")
    assert llm.status()["exhausted"]["research"] is True


def test_llm_retries_overloaded_models_then_reports_busy(monkeypatch):
    from google.genai import errors
    from outreach import llm, research
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_RPM", "100000")
    monkeypatch.setenv("GEMINI_DRAFT_MODELS", "m-a,m-b")
    monkeypatch.setattr(llm, "_skip", set())
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    brief = research.Brief(company_summary="s", facts=[], pains=[], best_hook="h", proof_id="re_pipeline",
                           angle="a", contact_first_name="", contact_role="Founder", fit_score=8, fit_reason="r")
    overloaded = errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
    calls = []

    def gen(model, contents, config):
        calls.append(model)
        if model == "m-b" and calls.count("m-b") > 2:        # recovers on the second round
            return mock.MagicMock(parsed=brief)
        raise overloaded
    fake = mock.MagicMock()
    fake.models.generate_content.side_effect = gen
    monkeypatch.setattr(llm, "_client", fake)
    assert llm.generate("s", "p", research.Brief).fit_score == 8

    fake.models.generate_content.side_effect = lambda **k: (_ for _ in ()).throw(overloaded)
    with pytest.raises(llm.ModelsBusy, match="m-a: busy, m-b: busy"):
        llm.generate("s", "p", research.Brief)
    assert llm.last_stop == "busy"
    assert llm.status()["out"] == {}                          # overloads never mark a model as used up
