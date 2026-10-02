"""Leads tab, Approve all, Run all, Draft email, every reply in Respond, Clear activity, and one lead
travelling the whole pipeline: found -> enriched -> verified -> researched -> drafted -> approved -> sent."""
import json
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import db
    db.init()


def _lead(conn, n, **kw):
    from outreach import db
    f = dict(email=f"sara{n}@palm{n}.ae", domain=f"palm{n}.ae", company=f"Palm {n}", website=f"https://palm{n}.ae",
             segment="gulf_realestate", status="new", email_status="valid", email_source="website", source="osm")
    f.update(kw)
    db.add_lead(conn, **f)
    return conn.execute("SELECT id FROM leads WHERE domain=?", (f["domain"],)).fetchone()[0]


def test_every_lead_says_where_it_is_and_why(monkeypatch):
    from outreach import db, leadview, sender
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana")
    with db.connect() as conn:
        _lead(conn, 1, status="unfit", fit=3, research=json.dumps({"fit_reason": "500+ staff enterprise"}))
        _lead(conn, 2, status="invalid", email=None, email_status="invalid")
        lid = _lead(conn, 3, status="drafted", fit=8)
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, confidence) VALUES (?,0,'idea','Hi Sara, x',0.6)", (lid,))
        lid4 = _lead(conn, 4, status="drafted", fit=8, email_source="dashboard")
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, confidence) VALUES (?,0,'idea','Hi, x',0.9)", (lid4,))
        items = {x["company"]: x for x in leadview.items(conn)}
    assert items["Palm 1"]["group"] == "skipped" and "500+ staff" in items["Palm 1"]["why"] and items["Palm 1"]["can_draft"]
    assert items["Palm 2"]["why"].startswith("No email") and not items["Palm 2"]["can_draft"]
    assert items["Palm 3"]["check"] == "ok"                                    # your approval would be enough
    assert "public or provider-verified" in items["Palm 4"]["check"]


def test_draft_one_lead_researches_first(monkeypatch):
    from outreach import db, leadview, llm, personalize, research
    with db.connect() as conn:
        lid = _lead(conn, 1, status="verified")
    brief = research.mock_brief({"company": "Palm 1", "first_name": ""})
    monkeypatch.setattr(research, "news", lambda c: [])
    monkeypatch.setattr(llm, "generate", lambda *a, **k: brief)
    monkeypatch.setattr(personalize, "generate", lambda row, angle=None: personalize.Sequence(
        subject="palm leads", body="Hi Sara, a real idea.", followups=[personalize.FollowUp(body="f1"), personalize.FollowUp(body="f2")],
        linkedin_note="n", linkedin_dm="d", confidence=0.8, review_note=""))
    assert leadview.draft_one(lid) == "drafted: it's in Review now"
    with db.connect() as conn:
        row = conn.execute("SELECT status, fit FROM leads WHERE id=?", (lid,)).fetchone()
        assert row["status"] == "drafted" and row["fit"] == 8
    assert leadview.draft_one(lid).startswith("skipped")


def test_approve_all_and_run_all(monkeypatch):
    from outreach import dashboard_sync, db, engine
    with db.connect() as conn:
        ids = []
        for n in range(3):
            lid = _lead(conn, n, status="drafted")
            conn.execute("INSERT INTO messages (lead_id, step, subject, body) VALUES (?,0,'s','b')", (lid,))
            ids.append(lid)
    assert dashboard_sync._approve_many(1, {"ids": ids + [999]}) == "approved 3, skipped 1 (no longer drafts)"
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE status='approved' AND approved_by='you'").fetchone()[0] == 3
    started = []
    monkeypatch.setattr(engine, "spawn", lambda job: started.append(job) or "started")
    dashboard_sync._run_all(1, {})
    assert started == ["prepare", "community", "content"]


def test_respond_shows_every_reply_except_bounces():
    from outreach import dashboard_sync, db
    with db.connect() as conn:
        lid = _lead(conn, 1, status="replied")
        for i, cat in enumerate(["interested", "not_interested", "unsubscribe", "out_of_office", "bounce", "not_now"]):
            conn.execute("INSERT INTO replies (lead_id, message_id, category, received_at) VALUES (?,?,?,?)",
                         (lid, f"<m{i}>", cat, f"2026-10-0{i + 1}"))
        cats = [r["category"] for r in dashboard_sync._reply_items(conn)]
    assert cats == ["interested", "not_interested", "unsubscribe", "out_of_office", "not_now"]


def test_one_lead_goes_all_the_way_from_found_to_sent(monkeypatch):
    """The whole pipeline with the network stubbed: nothing between finding and sending may drop the lead."""
    from outreach import dashboard_sync, db, enrich, llm, personalize, research, sender, transport, verify
    with db.connect() as conn:
        lid = _lead(conn, 1, email=None, email_status="unchecked", email_source="")
    monkeypatch.setattr(enrich, "analyse", lambda url: ({"reachable": True, "emails_on_site": ["sara1@palm1.ae"],
                                                          "whatsapp_link": True, "portals": True}, "Dubai brokerage"))
    assert enrich.run(10) == 1
    monkeypatch.setattr(verify, "has_mx", lambda d: True)
    verify.run()
    brief = research.Brief(company_summary="Dubai brokerage", facts=[], pains=[], best_hook="WhatsApp only",
                           proof_id="re_pipeline", angle="a", contact_first_name="Sara", contact_role="Owner",
                           fit_score=8, fit_reason="clear need")
    monkeypatch.setattr(research, "news", lambda c: [])
    monkeypatch.setattr(llm, "generate", lambda *a, **k: brief)
    assert research.run(10)["researched"] == 1
    monkeypatch.setattr(personalize, "generate", lambda row, angle=None: personalize.Sequence(
        subject="palm whatsapp leads", body="Hi Sara, your listings send buyers to WhatsApp.",
        followups=[personalize.FollowUp(body="f1"), personalize.FollowUp(body="f2")],
        linkedin_note="n", linkedin_dm="d", confidence=0.7, review_note=""))
    assert personalize.run(5) == 1
    assert dashboard_sync._approve_many(1, {"ids": [lid]}) == "approved 1"
    box = {"email": "me@zoho.in", "max_per_day": 35}
    monkeypatch.setattr(sender.config, "inboxes", lambda: [box])
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana")
    sent = []
    monkeypatch.setattr(transport, "send", lambda b, to, subject, body, thread: sent.append((to, subject)) or ("<m1>", "z1"))
    with db.connect() as conn:
        db.set_state(conn, "last_inbound_sync:me@zoho.in", datetime.now(timezone.utc).isoformat())
    assert sender.tick(5) == 1
    assert sent == [("sara1@palm1.ae", "palm whatsapp leads")]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id=?", (lid,)).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE lead_id=? AND step>0 AND due_at LIKE '20%'", (lid,)).fetchone()[0] == 2


def test_guessed_founder_emails_get_confirmed_or_parked(monkeypatch):
    from outreach import db, verify
    from outreach.prospecting.models import ProspectResult
    with db.connect() as conn:
        a = _lead(conn, 1, status="verified", email="priya@palm1.ae", email_status="guessed", first_name="Priya", last_name="Nair")
        b = _lead(conn, 2, status="verified", email="bo@palm2.ae", email_status="guessed", first_name="Bo", last_name="Li")
    answers = {"palm1.ae": ProspectResult(final_email="priya.nair@palm1.ae", confidence_score=93, confidence_level="verified",
                                          source="provider_prospeo", verification_provider="prospeo"),
               "palm2.ae": ProspectResult(final_email="bo@palm2.ae", confidence_score=50, confidence_level="unresolved")}
    monkeypatch.setattr("outreach.prospecting.pipeline.processor.enrich_prospect", lambda d: answers[d["domain"]])
    assert verify.confirm_guesses() == {"confirmed": 1, "unconfirmed": 1}
    with db.connect() as conn:
        ra = conn.execute("SELECT * FROM leads WHERE id=?", (a,)).fetchone()
        rb = conn.execute("SELECT * FROM leads WHERE id=?", (b,)).fetchone()
    assert (ra["email"], ra["email_status"], ra["email_source"]) == ("priya.nair@palm1.ae", "valid", "provider_verified")
    assert rb["email_status"] == "unconfirmed"
    assert verify.confirm_guesses() == {"confirmed": 0, "unconfirmed": 0}     # never paid for twice
