"""Held emails are visible with a reason, can be overridden, provider refusals retry, and uncertain
sends wait for your decision. Plus sending found prospects into the engine."""
import smtplib
from datetime import datetime, timezone
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import db, sender
    db.init()
    monkeypatch.setattr(sender.config, "inboxes", lambda: [{"email": "me@zoho.in", "max_per_day": 35}])
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana")
    monkeypatch.setattr(sender, "_choose_inbox", lambda *a: {"email": "me@zoho.in"})
    with db.connect() as conn:
        db.set_state(conn, "last_inbound_sync:me@zoho.in", datetime.now(timezone.utc).isoformat())


def _approved(n=1, approved_by="you", confidence=0.7, **lead):
    from outreach import db
    fields = dict(email=f"sara{n}@palm{n}.ae", domain=f"palm{n}.ae", company=f"Palm {n}", segment="gulf_realestate",
                  status="approved", email_status="valid", email_source="website", fit=8)
    fields.update(lead)
    with db.connect() as conn:
        db.add_lead(conn, **fields)
        lid = conn.execute("SELECT id FROM leads WHERE email=?", (fields["email"],)).fetchone()[0]
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, confidence, approved_by) "
                     "VALUES (?,0,'idea','Hi Sara, a specific idea.','approved',?,?)", (lid, confidence, approved_by))
        return lid, conn.execute("SELECT id FROM messages WHERE lead_id=?", (lid,)).fetchone()[0]


def _msg(mid):
    from outreach import db
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())


def test_your_approval_is_enough_for_low_confidence_and_generic_addresses(monkeypatch):
    from outreach import sender, transport
    sent = []
    monkeypatch.setattr(transport, "send", lambda box, to, *a: sent.append(to) or ("<m>", "z"))
    _approved(1, confidence=0.6)
    _approved(2, email="info@palm2.ae", email_status="risky")
    assert sender.tick(5) == 2 and sorted(sent) == ["info@palm2.ae", "sara1@palm1.ae"]


def test_every_segment_sends_unless_you_limit_it(monkeypatch):
    from outreach import config, sender, transport
    monkeypatch.setattr(transport, "send", lambda *a: ("<m>", "z"))
    assert "allowed_segments" not in config.settings()["sending"]
    _approved(1, segment="intl_freelance_posts")
    assert sender.tick(5) == 1


def test_held_emails_show_the_reason_and_send_anyway_works(monkeypatch):
    from outreach import dashboard_sync, db, sender, transport
    sent = []
    monkeypatch.setattr(transport, "send", lambda box, to, *a: sent.append(to) or ("<m>", "z"))
    _, guessed = _approved(1, email_source="dashboard")
    _, no_ai = _approved(2, site_text="No AI-generated applications will be reviewed.")
    _, auto = _approved(3, approved_by="auto", confidence=0.6)
    assert sender.tick(5) == 0
    with db.connect() as conn:
        held = {h["id"]: h for h in dashboard_sync._held_items(conn)}
    assert "came from dashboard" in held[guessed]["hold"] and held[guessed]["overridable"]
    assert "AI-written" in held[no_ai]["hold"] and not held[no_ai]["overridable"]
    assert "85%" in held[auto]["hold"]
    assert dashboard_sync._send_anyway(guessed, {}).startswith("will send")
    assert dashboard_sync._send_anyway(no_ai, {}).startswith("will send")   # the button isn't shown, but even so:
    assert sender.tick(5) == 1 and sent == ["sara1@palm1.ae"]               # the no-AI request still wins


def test_provider_refusal_retries_then_holds(monkeypatch):
    from outreach import sender, transport
    refuse = transport.ZohoError("POST /messages: HTTP 400, API status 400", 400)
    monkeypatch.setattr(transport, "send", mock.Mock(side_effect=refuse))
    _, mid = _approved()
    for _ in range(3):
        sender.tick(5)
    m = _msg(mid)
    assert m["status"] == "approved" and m["attempts"] == 3
    sender.tick(5)
    m = _msg(mid)
    assert transport.send.call_count == 3 and "refused it 3 times" in m["hold"]


def test_login_failure_sends_nothing_and_marks_nothing_uncertain(monkeypatch):
    from outreach import sender, transport
    monkeypatch.setattr(transport, "ready", mock.Mock(side_effect=transport.ZohoError("token refresh failed")))
    monkeypatch.setattr(transport, "send", mock.Mock())
    _, mid = _approved()
    assert sender.tick(5) == 0
    assert not transport.send.called and _msg(mid)["status"] == "approved"


def test_uncertain_send_waits_for_your_decision(monkeypatch):
    from outreach import dashboard_sync, db, sender, transport
    monkeypatch.setattr(transport, "send", mock.Mock(side_effect=smtplib.SMTPServerDisconnected("dropped")))
    pings = []
    monkeypatch.setattr(sender.replies, "notify", pings.append)
    lid, mid = _approved()
    with db.connect() as conn:
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, due_at) VALUES (?,1,'Re: idea','x','approved','4')", (lid,))
    sender.tick(5)
    assert _msg(mid)["status"] == "needs_reconciliation" and "Not sure" in pings[0]
    with db.connect() as conn:
        assert [h["kind"] for h in dashboard_sync._held_items(conn)] == ["uncertain"]
    assert dashboard_sync._mark_sent(mid, {}).startswith("marked as sent")
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id=?", (lid,)).fetchone()[0] == "active"
        assert "T" in conn.execute("SELECT due_at FROM messages WHERE lead_id=? AND step=1", (lid,)).fetchone()[0]


def test_found_prospect_goes_into_the_engine():
    from outreach import dashboard_sync, db
    with db.connect() as conn:
        conn.execute("INSERT INTO prospects (first_name, last_name, full_name, company, domain, final_email, "
                     "confidence_level, source) VALUES ('Priya','Nair','Priya Nair','Tessel','tessel.ai',"
                     "'priya@tessel.ai','verified','company_pattern')")
        conn.execute("INSERT INTO prospects (full_name, company, domain, final_email, confidence_level) "
                     "VALUES ('X','Y','y.com','x@y.com','uncertain')")
    assert dashboard_sync._promote_prospect(1, {"segment": "intl_startups_intern"}).startswith("sent to the engine")
    assert dashboard_sync._promote_prospect(2, {"segment": "intl_startups_intern"}).startswith("error")
    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE email='priya@tessel.ai'").fetchone()
    assert lead["status"] == "new" and lead["first_name"] == "Priya"   # its website is read first
    assert lead["email_source"] == "prospecting:company_pattern"   # a pattern guess: held until you say send
