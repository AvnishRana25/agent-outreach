"""Guards that protect replies and reputation: unread replies surface, reply quota is reserved,
drafting matches send capacity, template text never goes out, internship wording."""
from datetime import datetime, timezone
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import db
    db.init()


def _lead(conn, n=1, **kw):
    from outreach import db
    fields = dict(email=f"omar{n}@palm{n}.ae", domain=f"palm{n}.ae", company=f"Palm {n}", first_name="Omar",
                  segment="gulf_realestate", status="active", email_status="valid")
    fields.update(kw)
    db.add_lead(conn, **fields)
    return conn.execute("SELECT id FROM leads WHERE email=?", (fields["email"],)).fetchone()[0]


def _incoming(addr="omar1@palm1.ae", mid="<r1@x>", pid="z9"):
    from outreach import transport
    return transport.Incoming(message_id=mid, from_addr=addr, from_header="Omar", subject="Re: x",
                              body="Yes, send the plan please", received_at="2026-10-19T08:00:00+00:00", provider_id=pid)


def test_reply_gemini_cannot_read_still_reaches_you(monkeypatch):
    from outreach import dashboard_sync, db, llm, replies, transport
    with db.connect() as conn:
        _lead(conn)
    monkeypatch.setattr(replies.config, "inboxes", lambda: [{"email": "me@zoho.in", "transport": "zoho_api"}])
    monkeypatch.setattr(transport, "fetch", lambda box, days, known=None: [_incoming()])
    monkeypatch.setattr(replies, "classify", mock.Mock(side_effect=llm.QuotaExhausted("out")))
    pings = []
    monkeypatch.setattr(replies, "notify", pings.append)
    assert replies.sync(4) == 1
    assert pings and "could be a yes" in pings[0]
    with db.connect() as conn:
        items = dashboard_sync._reply_items(conn)
    assert [i["category"] for i in items] == ["unclassified"]

    # quota is back: the next sync reads it, and a yes gets the hot-lead ping
    monkeypatch.setattr(transport, "fetch", lambda box, days, known=None: [])
    monkeypatch.setattr(replies, "classify", lambda lead, s, b: replies.ReplyClass(
        category="interested", summary="wants the plan", suggested_reply="Great, here it is."))
    replies.sync(4)
    with db.connect() as conn:
        assert conn.execute("SELECT category FROM replies").fetchone()[0] == "interested"
    assert "INTERESTED" in pings[-1]


def test_stored_messages_are_not_downloaded_again(monkeypatch):
    from outreach import transport
    z = transport.Zoho({"email": "me@zoho.in", "zoho_dc": "in"})
    z._account_id, z._spam = "1", ""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    calls = []

    def call(method, path, **kw):
        calls.append(path)
        if path.endswith("/messages/view"):
            return {"data": [{"messageId": "old", "folderId": "f", "receivedTime": now_ms, "fromAddress": "a@b.com"},
                             {"messageId": "new", "folderId": "f", "receivedTime": now_ms, "fromAddress": "c@d.com"}]}
        return {"data": {"content": "hi", "headerContent": ""}}
    monkeypatch.setattr(z, "call", call)
    got = z.fetch(4, known={"old"})
    assert [m.provider_id for m in got] == ["new"]
    assert not any("/old/" in c for c in calls)


def test_side_work_leaves_reply_quota_alone(monkeypatch):
    from outreach import llm
    monkeypatch.setattr(llm, "_skip", set())
    used = {m: 15 for m in llm.chain("reply")}                     # 20/day limit, 5 kept for replies
    monkeypatch.setattr(llm, "_state", lambda update=None: {"calls": dict(used), "limits": {}, "out": {}})
    tried = []
    monkeypatch.setattr(llm, "_try", lambda m, *a: tried.append(m) or ("ok", "result"))
    with pytest.raises(llm.QuotaExhausted):
        llm.generate("s", "p", object, kind="community")            # community posts: stop at the reserve
    assert tried == []
    assert llm.generate("s", "p", object, kind="reply") == "result"  # replies to your emails still get through
    research_models = [m for m in llm.chain("research") if m not in used]
    assert llm.generate("s", "p", object, kind="research") == "result" and tried[-1] in research_models


def test_drafting_matches_what_can_be_sent(monkeypatch):
    from outreach import db, sender
    boxes = [{"email": "me@zoho.in", "max_per_day": 35, "warmup_start": "2026-10-01"}]
    monkeypatch.setattr(sender.config, "inboxes", lambda: boxes)
    now = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)          # week 1: 10 a day
    assert sender.draft_room(now=now)["room"] == 20
    with db.connect() as conn:
        for i in range(15):
            _lead(conn, i, status="drafted")
    r = sender.draft_room(now=now)
    assert r["room"] == 5 and r["waiting"] == 15


def test_template_text_is_never_sent(monkeypatch):
    from outreach import db, sender, transport
    with db.connect() as conn:
        lid = _lead(conn, status="approved")
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status) VALUES (?,0,'idea','Hi [First Name], x','approved')", (lid,))
        lid2 = _lead(conn, 2, status="approved")
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status) VALUES (?,0,'idea','Hi Omar, x','approved')", (lid2,))
    monkeypatch.setattr(sender.config, "inboxes", lambda: [{"email": "me@zoho.in", "max_per_day": 35}])
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish\ngithub.com/YOUR-GITHUB")
    monkeypatch.setattr(transport, "send", mock.Mock(side_effect=AssertionError("sent template text")))
    assert sender.tick(5) == 0
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id=?", (lid,)).fetchone()[0] == "drafted"  # back to Review
        assert "YOUR-GITHUB" in conn.execute("SELECT error FROM messages WHERE lead_id=?", (lid2,)).fetchone()[0]


def test_profile_placeholders_are_listed():
    from outreach import config
    found = config.placeholders()                  # the repo has only the example profile
    assert any("YOUR-GITHUB" in x for x in found) and any("calendar_link" in x for x in found)
    assert not config.PLACEHOLDER.search("Happy to help your team; see for yourself.")


def test_internship_emails_may_say_internship():
    from outreach import personalize
    intern = personalize.system_prompt("india_startups_intern")
    freelance = personalize.system_prompt("gulf_realestate")
    assert '"intern"' not in intern and "internship or contract role" in intern
    assert '"intern"' in freelance and '"student"' in freelance and '"student"' in intern
