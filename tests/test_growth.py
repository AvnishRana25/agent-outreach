"""Deal tracking, the one-page plan, LinkedIn post drafts, A/B angles and the weekly digest (offline)."""
import json

import pytest


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import db
    db.init()


def _lead(conn, n, segment="gulf_realestate", angle="", sent=True, reply=None):
    from outreach import db
    db.add_lead(conn, email=f"p{n}@co{n}.ae", domain=f"co{n}.ae", company=f"Co {n}", segment=segment,
                status="active", angle=angle)
    lid = conn.execute("SELECT id FROM leads WHERE email=?", (f"p{n}@co{n}.ae",)).fetchone()[0]
    if sent:
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status) VALUES (?,0,'s','b','sent')", (lid,))
    if reply:
        conn.execute("INSERT INTO replies (lead_id, inbox, category, received_at, summary, body, subject, from_addr) "
                     "VALUES (?, 'me@zoho.in', ?, ?, 'asks for price', 'What does it cost?', 'Re: s', ?)",
                     (lid, reply, db.now(), f"p{n}@co{n}.ae"))
    return lid


def test_angles_alternate_per_segment():
    from outreach import db, personalize
    with db.connect() as conn:
        picks = []
        for i in range(4):
            a = personalize.pick_angle(conn, "gulf_realestate")
            picks.append(a["id"])
            _lead(conn, i, angle=a["id"])
        assert picks == ["speed", "attribution", "speed", "attribution"]
        assert "Angle for this email" in personalize.system_prompt("gulf_realestate", {"focus": "Speed."})
        assert "USD 350" in personalize.system_prompt("gulf_realestate")


def test_deal_stages_and_pipeline():
    from outreach import db, growth, report
    with db.connect() as conn:
        a = _lead(conn, 1, reply="question")
        _lead(conn, 2)                                       # sent, no reply: not in the pipeline
    assert growth.set_stage(a, "won", 350.0, "paid on go-live") == "stage: Won"
    assert growth.set_stage(a, "bogus").startswith("error")
    with db.connect() as conn:
        items = growth.pipeline_items(conn)
        assert [i["company"] for i in items] == ["Co 1"] and items[0]["deal_value"] == 350.0
        all_row = [r for r in report.funnel(conn) if r["name"] == "ALL"][0]
        assert (all_row["won"], all_row["won_value"], all_row["calls"]) == (1, 350.0, 1)


def test_make_plan_uses_offer_price(monkeypatch):
    from outreach import db, growth, llm
    with db.connect() as conn:
        lid = _lead(conn, 1, reply="question")
        rid = conn.execute("SELECT id FROM replies").fetchone()[0]
    seen = {}

    def fake(system, prompt, schema, **kw):
        seen["system"], seen["prompt"] = system, prompt
        return growth.Plan(title="WhatsApp lead engine for Co 1", situation="Leads arrive on WhatsApp.",
                           steps=["Map the current flow", "Instant reply", "Assign agents"], timeline="Live in 7 days",
                           price="USD 350 fixed", needs=["WhatsApp access"], next_step="A 20-minute call.")
    monkeypatch.setattr(llm, "generate", fake)
    assert growth.make_plan(rid) == "plan ready"
    assert "USD 350" in seen["system"] and "What does it cost?" in seen["prompt"]
    with db.connect() as conn:
        plan = conn.execute("SELECT plan FROM replies WHERE id=?", (rid,)).fetchone()[0]
    assert plan.startswith("WhatsApp lead engine for Co 1") and "1. Map the current flow" in plan
    assert lid


def test_linkedin_posts_rotate_proof_points(monkeypatch):
    from outreach import config, db, growth, llm
    calls = []

    def fake(system, prompt, schema, **kw):
        ids = [line.split("]")[0].split("[")[1] for line in prompt.splitlines() if line.startswith("- [")]
        calls.append(ids)
        return growth.Posts(posts=[growth.Post(proof_id=i, text=f"post about {i}") for i in ids])
    monkeypatch.setattr(llm, "generate", fake)
    growth.linkedin_posts()
    growth.linkedin_posts()
    assert len(calls[0]) == 3 and not set(calls[0]) & set(calls[1])   # next week uses different proofs
    with db.connect() as conn:
        posts = json.loads(db.get_state(conn, "content:linkedin_posts"))["posts"]
    assert [p["proof_id"] for p in posts] == calls[1]
    assert len(config.profile()["proof_points"]) >= 6


def test_weekly_digest_suggests_changes(monkeypatch):
    from outreach import db, growth, replies
    sent = []
    monkeypatch.setattr(replies, "notify", lambda text: sent.append(text))
    with db.connect() as conn:
        for i in range(30):                                   # angle 'speed': 3 replies in 30
            _lead(conn, i, angle="speed", reply="interested" if i < 3 else None)
        for i in range(30, 60):                               # angle 'attribution': none
            _lead(conn, i, angle="attribution")
        for i in range(60, 105):                              # UK: 45 sent, no replies
            _lead(conn, i, segment="uk_agencies")
    growth.weekly_digest()
    text = sent[0]
    assert "angle 'speed' gets 10.0% replies vs 'attribution' 0.0%" in text
    assert "uk_agencies: 45 sent, 0% replies" in text
    assert "3 replies are still waiting on you" in text


def test_settings_fill_missing_keys_but_keep_yours(tmp_path, monkeypatch):
    import shutil
    from outreach import config
    shutil.copy(config.CONFIG_DIR / "settings.example.yaml", tmp_path / "settings.example.yaml")
    (tmp_path / "settings.yaml").write_text(
        "segments:\n  gulf_realestate:\n    daily_new: 20\n    offer: my own offer\n"
        "    audience: a\n    pain: b\n    cta: c\n")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    config.settings.cache_clear()
    try:
        s = config.settings()
        seg = s["segments"]["gulf_realestate"]
        assert seg["daily_new"] == 20 and seg["offer"] == "my own offer"      # yours wins
        assert [a["id"] for a in seg["angles"]] == ["speed", "attribution"]   # new keys appear
        assert "uk_agencies" not in s["segments"]                             # deleted segments stay deleted
        assert s["firecrawl"]["daily_credit_cap"] == 60                       # new top-level sections appear
    finally:
        config.settings.cache_clear()
