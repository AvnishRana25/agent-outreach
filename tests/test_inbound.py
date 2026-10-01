"""Inbox checks that must pass before automatic sending."""
from datetime import datetime, timezone
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "inbound.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import db
    db.init()


def test_zoho_oauth_uses_body_and_error_hides_secrets(monkeypatch):
    from outreach import transport
    monkeypatch.setenv("ZOHO_CLIENT_ID", "client-secret-id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "refresh-secret")
    box = {"email": "me@zoho.in"}
    z = transport.Zoho(box)
    post = mock.Mock(return_value=mock.Mock(json=lambda: {"error": "invalid_code", "token": "refresh-secret"}))
    monkeypatch.setattr(transport.requests, "post", post)
    with pytest.raises(transport.ZohoError) as exc:
        z.token()
    assert post.call_args.kwargs["data"]["refresh_token"] == "refresh-secret"
    assert "params" not in post.call_args.kwargs
    assert "refresh-secret" not in str(exc.value)
    z.exchange_code("grant-secret")
    assert post.call_args.kwargs["data"]["code"] == "grant-secret"


def test_zoho_fetch_reads_second_page(monkeypatch):
    from outreach import transport
    z = transport.Zoho({"email": "me@zoho.in"})
    z._account_id, z._spam = "1", ""
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    first = [{"messageId": str(i), "folderId": "f", "receivedTime": now,
              "fromAddress": "other@x.com"} for i in range(200)]
    last = {"messageId": "200", "folderId": "f", "receivedTime": now,
            "fromAddress": "other@x.com"}
    starts = []

    def call(method, path, **kw):
        if path.endswith("/messages/view"):
            starts.append(kw["params"]["start"])
            return {"data": first if starts[-1] == 1 else [last]}
        return {"data": {"content": "hi", "headerContent": ""}}

    monkeypatch.setattr(z, "call", call)
    assert [m.provider_id for m in z.fetch(4, known={str(i) for i in range(200)})] == ["200"]
    assert starts == [1, 201]


def test_zoho_folder_lookup_failure_does_not_claim_inbox_synced(monkeypatch):
    from outreach import transport
    z = transport.Zoho({"email": "me@zoho.in"})
    z._account_id = "1"
    monkeypatch.setattr(z, "call", mock.Mock(side_effect=transport.ZohoError("folder API failed")))
    with pytest.raises(transport.ZohoError):
        z.fetch(1)


def test_reply_matches_thread_or_exact_sender_only(monkeypatch):
    from outreach import db, replies, transport
    with db.connect() as conn:
        db.add_lead(conn, email="a@firm.com", domain="firm.com", segment="gulf_realestate", status="active")
        lead = conn.execute("SELECT id FROM leads").fetchone()[0]
        conn.execute("INSERT INTO messages (lead_id,step,message_id,status) VALUES (?,0,'<sent@x>','sent')", (lead,))
        colleague = transport.Incoming("<reply@x>", "b@firm.com", "B", "Re: hello", "yes", "", refs="<sent@x>")
        unrelated = transport.Incoming("<other@x>", "b@firm.com", "B", "hello", "yes", "")
        assert replies._match_lead(conn, colleague, False)["id"] == lead
        assert replies._match_lead(conn, unrelated, False) is None


def test_sync_stamps_only_readable_inbox_and_drafts_to_sender(monkeypatch):
    from outreach import db, replies, transport
    box = {"email": "me@zoho.in", "transport": "zoho_api"}
    monkeypatch.setattr(replies.config, "inboxes", lambda: [box])
    with db.connect() as conn:
        db.add_lead(conn, email="a@firm.com", domain="firm.com", segment="gulf_realestate", status="active")
        lead = conn.execute("SELECT id FROM leads").fetchone()[0]
        conn.execute("INSERT INTO messages (lead_id,step,message_id,status) VALUES (?,0,'<sent@x>','sent')", (lead,))
    incoming = transport.Incoming("<reply@x>", "b@firm.com", "B", "Re: hello", "yes", "", refs="<sent@x>")
    monkeypatch.setattr(transport, "fetch", lambda *a: [incoming])
    monkeypatch.setattr(replies, "classify", lambda *a: replies.ReplyClass(
        category="interested", summary="yes", suggested_reply="Thanks"))
    monkeypatch.setattr(replies, "notify", lambda *a: None)
    drafts = []
    monkeypatch.setattr(transport, "save_draft", lambda box, to, *a: drafts.append(to) or True)
    assert replies.sync() == 1
    assert drafts == ["b@firm.com"]
    with db.connect() as conn:
        assert datetime.fromisoformat(db.get_state(conn, "last_inbound_sync:me@zoho.in"))
    monkeypatch.setattr(transport, "fetch", mock.Mock(side_effect=transport.InboxUnavailable("offline")))
    with pytest.raises(transport.InboxUnavailable):
        replies.sync(require_all=True, triage=False)


def test_smtp_without_imap_cannot_claim_synced():
    from outreach import transport
    with pytest.raises(transport.InboxUnavailable):
        transport.fetch({"email": "me@example.com", "transport": "smtp"}, 4)


def test_self_addressed_zoho_sends_threads_and_stops_after_reply(monkeypatch):
    from outreach import db, replies, sender, transport
    box = {"email": "me@zoho.in", "transport": "zoho_api", "max_per_day": 35}
    monkeypatch.setattr(sender.config, "inboxes", lambda: [box])
    monkeypatch.setattr(replies.config, "inboxes", lambda: [box])
    monkeypatch.setattr(sender.config, "settings", lambda: {
        "sending": {"allowed_segments": ["gulf_realestate"], "home_timezone": "Asia/Kolkata"},
        "sequence": {"followup_days": [4, 10]},
        "segments": {"gulf_realestate": {"timezone": "Asia/Dubai", "send_days": [1, 2, 3, 4, 5],
                                         "send_windows": ["09:00-17:00"]}}
    })
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana")

    with db.connect() as conn:
        db.set_state(conn, "last_inbound_sync:me@zoho.in", datetime.now(timezone.utc).isoformat())
        db.add_lead(conn, email="lead@firm.com", domain="firm.com", segment="gulf_realestate",
                    status="approved", fit=8, email_status="valid", email_source="website")
        lead_id = conn.execute("SELECT id FROM leads WHERE email='lead@firm.com'").fetchone()[0]
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, confidence) "
                     "VALUES (?, 0, 'Project idea', 'Hi, idea for you.', 'approved', 0.9)", (lead_id,))
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, confidence, due_at) "
                     "VALUES (?, 1, 'Re: Project idea', 'Following up on this.', 'approved', 0.9, '4')", (lead_id,))

    sent_calls = []
    def mock_send(b, to, subj, body, thread):
        sent_calls.append((to, subj, body, thread))
        if thread:
            return ("<zoho-followup-mid>", "zoho-followup-pid")
        return ("<zoho-step0-mid>", "zoho-step0-pid")

    monkeypatch.setattr(transport, "send", mock_send)

    # 1. Step 0 sends via Zoho
    assert sender.tick(1) == 1
    assert len(sent_calls) == 1
    assert sent_calls[0][0] == "lead@firm.com"
    assert sent_calls[0][3] is None  # no thread on first email

    with db.connect() as conn:
        m0 = conn.execute("SELECT status, message_id, provider_id FROM messages WHERE lead_id=? AND step=0", (lead_id,)).fetchone()
        assert tuple(m0) == ("sent", "<zoho-step0-mid>", "zoho-step0-pid")
        lead = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
        assert lead[0] == "active"

    # 2. Reply arrives referencing step 0
    incoming = transport.Incoming("<reply-mid>", "lead@firm.com", "Lead", "Re: Project idea",
                                  "Let's talk on Monday.", "", refs="<zoho-step0-mid>")
    monkeypatch.setattr(transport, "fetch", lambda *a: [incoming])
    monkeypatch.setattr(replies, "classify", lambda *a: replies.ReplyClass(
        category="interested", summary="wants to talk", suggested_reply="Great, Monday works."))
    monkeypatch.setattr(replies, "notify", lambda *a: None)
    monkeypatch.setattr(transport, "save_draft", lambda *a: True)

    assert replies.sync() == 1

    with db.connect() as conn:
        lead = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
        assert lead[0] == "replied"
        m1 = conn.execute("SELECT status FROM messages WHERE lead_id=? AND step=1", (lead_id,)).fetchone()
        assert m1[0] == "cancelled"

    # 3. Next sender tick runs: follow-up is stopped!
    assert sender.tick(1) == 0
    assert len(sent_calls) == 1  # no new send was attempted!
