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


def test_deal_currencies_and_next_actions_do_not_mix_totals():
    from outreach import db, growth, report
    with db.connect() as conn:
        uk = _lead(conn, 11, segment="uk_agencies")
        india = _lead(conn, 12, segment="india_realestate")
    assert growth.set_stage(uk, "won", 200, currency="GBP", next_action="Ask for referral",
                            next_due="2026-10-10") == "stage: Won"
    assert growth.set_stage(india, "won", 25000, currency="INR") == "stage: Won"
    with db.connect() as conn:
        items = growth.pipeline_items(conn)
        assert any(x["deal_next_action"] == "Ask for referral" and x["deal_next_due"] == "2026-10-10"
                   for x in items)
        total = next(x for x in report.funnel(conn) if x["name"] == "ALL")
        assert total["won_value"] is None
        assert total["won_values"] == {"GBP": 200.0, "INR": 25000.0}


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


def test_linkedin_posts_are_about_ai_news_industry_and_building(monkeypatch):
    from outreach import db, growth, llm
    news = [{"title": "Lab ships open-weight agent model", "url": "https://news.example/agents", "source": "HN",
             "date": "2026-10-01"}]
    monkeypatch.setattr(growth, "ai_news", lambda: news)
    seen = {}

    def fake(system, prompt, schema, **kw):
        seen.update(system=system, prompt=prompt)
        return growth.Posts(posts=[
            growth.Post(kind="news", text="take", sources=["https://news.example/agents", "https://made.up/x"],
                        proof_id="", check=""),
            growth.Post(kind="industry", text="opinion", sources=[], proof_id="", check="'evals beat models' is new"),
            growth.Post(kind="build", text="lesson", sources=[], proof_id="re_llm_backfill", check="")])
    monkeypatch.setattr(llm, "generate", fake)
    assert growth.linkedin_posts().startswith("3 LinkedIn post drafts")
    assert "Lab ships open-weight agent model" in seen["prompt"]
    assert "At most ONE of the three may mention real estate" in seen["system"]
    assert "NEVER describe Caudal AI's projects" in seen["system"]
    with db.connect() as conn:
        posts = json.loads(db.get_state(conn, "content:linkedin_posts"))["posts"]
    assert [p["kind"] for p in posts] == ["news", "industry", "build"]
    assert posts[0]["sources"] == ["https://news.example/agents"]          # links it didn't get are dropped
    assert posts[0]["news"][0]["title"] == "Lab ships open-weight agent model"


def test_linkedin_posts_use_your_opinions(monkeypatch):
    from outreach import config, growth
    profile = config.profile()
    monkeypatch.setattr(config, "profile", lambda: {**profile, "linkedin": {"opinions": ["Evaluation is the moat."]}})
    assert "- Evaluation is the moat." in growth._posts_system(config.profile())
    monkeypatch.setattr(config, "profile", lambda: {**profile, "linkedin": {}})
    assert "flag every opinion sentence" in growth._posts_system(config.profile())


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
