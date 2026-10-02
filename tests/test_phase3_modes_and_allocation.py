"""Tests for Phase 3: Separate FREELANCE and INTERNSHIP funnels, smarter 28/day allocation, and outcome analytics."""
import json
import os
from datetime import datetime, timezone
import pytest

from outreach import config, db, eligibility, personalize, scoring, sender, analytics, growth, replies
from outreach.scoring import (
    MODE_FREELANCE, MODE_INTERNSHIP, get_opportunity_mode,
    score_lead, OpportunityScore, calculate_contact_relevance,
    FREELANCE_WEIGHTS, INTERNSHIP_WEIGHTS
)


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    """Use a temporary SQLite database for every test."""
    p = tmp_path / "test_phase3.db"
    monkeypatch.setenv("OUTREACH_DB", str(p))
    monkeypatch.setenv("OUTREACH_ENV", "local")
    monkeypatch.delenv("ALLOW_UNUSED_QUOTA_REALLOCATION", raising=False)
    monkeypatch.delenv("DAILY_SEND_LIMIT", raising=False)
    monkeypatch.delenv("DAILY_FREELANCE_NEW_LIMIT", raising=False)
    monkeypatch.delenv("DAILY_INTERNSHIP_NEW_LIMIT", raising=False)
    monkeypatch.delenv("DAILY_FOLLOWUP_LIMIT", raising=False)
    db.init()
    yield p


def _create_lead(conn, **fields) -> int:
    defaults = {
        "email": "test@example.com",
        "first_name": "Test",
        "last_name": "User",
        "title": "Founder",
        "company": "Test Company",
        "website": "https://example.com",
        "domain": "example.com",
        "segment": "gulf_realestate",
        "opportunity_type": "contract",
        "email_status": "valid",
        "email_source": "website",
        "status": "approved",
        "score_total": 85,
        "fit": 8,
        "source_text": "Looking for WhatsApp automation developer 2026-10-01",
        "verified_evidence": json.dumps([
            {"fact": "WhatsApp CRM integration", "fact_type": "pain", "confidence": 0.9, "status": "VERIFIED_FACT"}
        ]),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    defaults.update(fields)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" * len(defaults))
    cur = conn.execute(f"INSERT INTO leads ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    return cur.lastrowid


def _create_message(conn, lead_id: int, step: int = 0, status: str = "approved", **fields) -> int:
    defaults = {
        "lead_id": lead_id,
        "step": step,
        "subject": "quick idea for test",
        "body": "Hi Test,\n\nI noticed your listings.\n\nBest,\nAvnish",
        "status": status,
        "confidence": 0.9,
        "approved_by": "avnish",
        "idempotency_key": f"lead:{lead_id}:step:{step}",
    }
    defaults.update(fields)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join("?" * len(defaults))
    cur = conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    return cur.lastrowid


# ==============================================================================
# 1. Opportunity Mode Detection
# ==============================================================================
def test_opportunity_mode_detection():
    # Explicit opportunity_type
    assert get_opportunity_mode({"opportunity_type": "contract"}) == MODE_FREELANCE
    assert get_opportunity_mode({"opportunity_type": "internship"}) == MODE_INTERNSHIP
    assert get_opportunity_mode({"opportunity_type": "intern"}) == MODE_INTERNSHIP
    assert get_opportunity_mode({"opportunity_type": "freelance"}) == MODE_FREELANCE

    # Inferred from segment
    assert get_opportunity_mode({"segment": "gulf_realestate"}) == MODE_FREELANCE
    assert get_opportunity_mode({"segment": "india_startups_intern"}) == MODE_INTERNSHIP

    # Explicit mode field
    assert get_opportunity_mode({"mode": "freelance"}) == MODE_FREELANCE
    assert get_opportunity_mode({"mode": "internship"}) == MODE_INTERNSHIP


# ==============================================================================
# 2. Mode-Specific Scoring Weights & OpportunityScore Mode
# ==============================================================================
def test_freelance_vs_internship_scoring_weights():
    fl_lead = {
        "company": "Agency Co",
        "segment": "uk_agencies",
        "opportunity_type": "contract",
        "title": "Managing Director",
        "source_text": "Need freelancer for Zapier workflow automation 2026-10-01",
        "site_text": "Digital marketing and web development agency",
        "email_status": "valid",
        "confidence": 90,
    }
    intern_lead = {
        "company": "Tech AI Labs",
        "segment": "india_startups_intern",
        "opportunity_type": "internship",
        "title": "Engineering Manager",
        "source_text": "Hiring intern for Python and LLM development 2026-10-01",
        "site_text": "AI startup building generative assistants",
        "email_status": "valid",
        "confidence": 90,
    }

    fl_score = score_lead(fl_lead)
    assert fl_score.mode == MODE_FREELANCE
    assert "[FREELANCE]" in fl_score.reason_summary
    assert fl_score.score_total >= 75

    intern_score = score_lead(intern_lead)
    assert intern_score.mode == MODE_INTERNSHIP
    assert "[INTERNSHIP]" in intern_score.reason_summary
    assert intern_score.score_total >= 75


# ==============================================================================
# 3. Contact Role Relevance per Mode
# ==============================================================================
def test_contact_role_relevance_per_mode():
    # In freelance mode:
    # Founder / CTO / Ops owner scored high (95-100)
    # Recruiter / HR scored low (30)
    score_founder, _ = calculate_contact_relevance({"title": "Founder & CEO", "opportunity_type": "contract"})
    assert score_founder == 100.0

    score_ops, _ = calculate_contact_relevance({"title": "Head of Operations", "opportunity_type": "contract"})
    assert score_ops == 95.0

    score_recruiter, _ = calculate_contact_relevance({"title": "Technical Recruiter", "opportunity_type": "contract"})
    assert score_recruiter == 30.0

    # In internship mode:
    # Technical Recruiter / Talent scored high (90)
    # Engineering Manager scored high (95)
    # Founder/CTO scored high (100)
    score_it_recruiter, _ = calculate_contact_relevance({"title": "Technical Recruiter", "opportunity_type": "internship"})
    assert score_it_recruiter == 90.0

    score_it_em, _ = calculate_contact_relevance({"title": "Engineering Manager", "opportunity_type": "internship"})
    assert score_it_em == 95.0


# ==============================================================================
# 4. Mode-Specific Messaging & Banned Phrases
# ==============================================================================
def test_mode_specific_drafting_guidelines_and_banned_phrases():
    # Check system prompt includes mode section
    fl_prompt = personalize.system_prompt("gulf_realestate", mode=MODE_FREELANCE)
    assert "# Mode Guidelines: FREELANCE" in fl_prompt
    assert "Problem -> evidence" in fl_prompt
    assert "CV-style pitching" in fl_prompt

    it_prompt = personalize.system_prompt("india_startups_intern", mode=MODE_INTERNSHIP)
    assert "# Mode Guidelines: INTERNSHIP" in it_prompt
    assert "Specific company/technical relevance" in it_prompt
    assert "passionate about" in it_prompt

    # Test banned phrases regex
    fl_banned = personalize.BANNED_FREELANCE_PHRASES[0]
    assert fl_banned.search("Please review my attached resume for this project")
    assert fl_banned.search("I have 5 years of experience in coding")
    assert fl_banned.search("Hire me to fix this")
    assert not fl_banned.search("I can outline how I would implement this workflow")

    it_banned = personalize.BANNED_INTERNSHIP_PHRASES[0]
    assert it_banned.search("I am passionate about machine learning")
    assert it_banned.search("I would love the opportunity to intern")
    assert it_banned.search("I admire your company and am eager to learn")
    assert not it_banned.search("I built a WhatsApp lead-routing workflow in Python")


# ==============================================================================
# 5. Daily 28-Email Allocation Defaults & Environment Overrides
# ==============================================================================
def test_daily_28_email_allocation_defaults(monkeypatch):
    alloc = config.allocation_settings()
    assert alloc["daily_send_limit"] == 28
    assert alloc["daily_new_limit"] == 28
    assert alloc["daily_freelance_new_limit"] in (12, 16)
    assert alloc["daily_internship_new_limit"] in (8, 12)
    assert alloc["daily_followup_limit"] in (0, 8)

    # Overrides via env
    monkeypatch.setenv("DAILY_SEND_LIMIT", "30")
    monkeypatch.setenv("DAILY_FREELANCE_NEW_LIMIT", "15")
    monkeypatch.setenv("DAILY_INTERNSHIP_NEW_LIMIT", "10")
    monkeypatch.setenv("DAILY_FOLLOWUP_LIMIT", "5")
    monkeypatch.setenv("ALLOW_UNUSED_QUOTA_REALLOCATION", "true")

    overridden = config.allocation_settings()
    assert overridden["daily_send_limit"] == 30
    assert overridden["daily_freelance_new_limit"] == 15
    assert overridden["daily_internship_new_limit"] == 10
    assert overridden["daily_followup_limit"] == 5
    assert overridden["allow_unused_quota_reallocation"] is True


# ==============================================================================
# 6. Daily Allocation Enforcement: Hard Ceiling (28 Sends)
# ==============================================================================
def test_daily_allocation_enforcement_hard_ceiling(monkeypatch):
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="lead29@test.com")
        msg_id = _create_message(conn, lead_id, step=0)
        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        # When 28 total sends already logged for today
        monkeypatch.setattr(db, "send_count", lambda conn, today, box, kind="first": 28 if kind == "first" else 0)
        res = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert res.eligible is False
        assert "quota" in res.failed_gates
        assert "Daily send ceiling reached: 28/28" in res.reasons["quota"].reason or "Daily new email ceiling reached" in res.reasons["quota"].reason


# ==============================================================================
# 7. Category Caps with Reallocation Disabled
# ==============================================================================
def test_daily_allocation_category_caps_no_reallocation(monkeypatch):
    monkeypatch.setenv("ALLOW_UNUSED_QUOTA_REALLOCATION", "false")
    monkeypatch.setenv("DAILY_FREELANCE_NEW_LIMIT", "12")
    monkeypatch.setattr(sender, "in_window", lambda *a: True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with db.connect() as conn:
        # Simulate 12 freelance first emails already sent today
        for i in range(12):
            db.bump_send_count(conn, today, "avnish@automatedoutreach.online", "first_freelance")
            db.bump_send_count(conn, today, "avnish@automatedoutreach.online", "first")

        counts = db.get_daily_allocation_counts(conn, today)
        assert counts["freelance"] == 12
        assert counts["total"] == 12

        # 13th freelance candidate should be blocked by quota gate
        fl_lead_id = _create_lead(conn, email="fl13@test.com", segment="gulf_realestate", opportunity_type="contract")
        fl_msg_id = _create_message(conn, fl_lead_id, step=0)
        fl_lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (fl_lead_id,)).fetchone())
        fl_msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (fl_msg_id,)).fetchone())

        res_fl = eligibility.evaluate_send_eligibility(fl_lead, fl_msg, conn)
        assert res_fl.eligible is False
        assert "quota" in res_fl.failed_gates
        assert "Daily freelance" in res_fl.reasons["quota"].reason and "12/12" in res_fl.reasons["quota"].reason

        # Meanwhile, internship category has space (0/8) and should pass!
        it_lead_id = _create_lead(conn, email="it1@test.com", segment="india_startups_intern", opportunity_type="internship")
        it_msg_id = _create_message(conn, it_lead_id, step=0)
        it_lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (it_lead_id,)).fetchone())
        it_msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (it_msg_id,)).fetchone())

        res_it = eligibility.evaluate_send_eligibility(it_lead, it_msg, conn)
        assert "quota" in res_it.passed_gates


# ==============================================================================
# 8. Quota Borrowing / Reallocation
# ==============================================================================
def test_daily_allocation_quota_borrowing(monkeypatch):
    monkeypatch.setenv("ALLOW_UNUSED_QUOTA_REALLOCATION", "true")
    monkeypatch.setattr(sender, "in_window", lambda *a: True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    box_email = "workwithavnish@zohomail.in"

    with db.connect() as conn:
        # Simulate 12 freelance first emails already sent today
        for i in range(12):
            db.bump_send_count(conn, today, box_email, "first_freelance")
            db.bump_send_count(conn, today, box_email, "first")

        counts = db.get_daily_allocation_counts(conn, today)
        assert counts["freelance"] == 12
        assert counts["total"] == 12  # well under 28

        # In reallocation mode: 13th freelance candidate CAN borrow from unused pool
        fl_lead_id = _create_lead(conn, email="fl13_realloc@test.com", segment="gulf_realestate", opportunity_type="contract")
        fl_msg_id = _create_message(conn, fl_lead_id, step=0)
        fl_lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (fl_lead_id,)).fetchone())
        fl_msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (fl_msg_id,)).fetchone())

        res_fl = eligibility.evaluate_send_eligibility(fl_lead, fl_msg, conn)
        assert "quota" in res_fl.passed_gates

        # But if total hits 28, even reallocation cannot exceed global 28 ceiling
        for i in range(16):
            db.bump_send_count(conn, today, box_email, "first")
        res_ceiling = eligibility.evaluate_send_eligibility(fl_lead, fl_msg, conn)
        assert res_ceiling.eligible is False
        assert "quota" in res_ceiling.failed_gates


# ==============================================================================
# 9. Sending Priority Ordering
# ==============================================================================
def test_sending_priority_ordering():
    with db.connect() as conn:
        # Create Priority B lead (score 76)
        l_b = _create_lead(conn, email="b@test.com", score_total=76, fit=7)
        m_b = _create_message(conn, l_b, step=0, confidence=0.8)

        # Create Priority A lead (score 92)
        l_a = _create_lead(conn, email="a@test.com", score_total=92, fit=9)
        m_a = _create_message(conn, l_a, step=0, confidence=0.95)

        # Create Priority A lead with higher score (95)
        l_top = _create_lead(conn, email="top@test.com", score_total=95, fit=9)
        m_top = _create_message(conn, l_top, step=0, confidence=0.98)

        candidates = sender._candidates(conn, datetime.now(timezone.utc).isoformat())
        first_ids = [c["id"] for c in candidates if c["step"] == 0]

        # Top Priority A should be first, then second Priority A, then Priority B
        assert first_ids[0] == m_top
        assert first_ids[1] == m_a
        assert first_ids[2] == m_b


# ==============================================================================
# 10. Outcome Tracking Across Lifecycle
# ==============================================================================
def test_message_outcomes_tracking():
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="client@test.com", company="Acme Client", title="CTO", segment="gulf_realestate")
        msg_id = _create_message(conn, lead_id, step=0)

        # 1. Record outcome on send
        row_id = db.record_message_outcome(
            conn,
            message_id=msg_id,
            lead_id=lead_id,
            lead_source="companies_house",
            source_type="website",
            mode="freelance",
            opportunity_score=88,
            company_stage="gulf_realestate",
            contact_role="CTO",
            email_confidence=95,
            evidence_confidence=0.9,
            personalization_confidence=0.9,
            message_angle="speed",
            cta_type="one_page_plan",
            subject_variant="acme workflow",
            sequence_variant="step_0",
            outcome="delivered"
        )
        assert row_id > 0
        conn.commit()

        # Verify recorded row
        outcome_row = conn.execute("SELECT * FROM message_outcomes WHERE id=?", (row_id,)).fetchone()
        assert outcome_row["outcome"] == "delivered"
        assert outcome_row["mode"] == "freelance"

        # 2. Update outcome on positive reply
        lead_dict = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        replies._apply(conn, lead_dict, replies.ReplyClass(category="interested", summary="Sounds good, let's talk", suggested_reply=""))
        conn.commit()
        outcome_updated = conn.execute("SELECT * FROM message_outcomes WHERE id=?", (row_id,)).fetchone()
        assert outcome_updated["outcome"] == "positive_reply"

        # 3. Update outcome on deal stage progression: call_booked -> meeting
        growth.set_stage(lead_id, "call_booked", note="Call set for Tuesday")
        outcome_deal = conn.execute("SELECT * FROM message_outcomes WHERE id=?", (row_id,)).fetchone()
        assert outcome_deal["outcome"] == "meeting"

        # 4. Update outcome on won deal -> won_project
        growth.set_stage(lead_id, "won", value=1500.0, note="Contract signed")
        outcome_won = conn.execute("SELECT * FROM message_outcomes WHERE id=?", (row_id,)).fetchone()
        assert outcome_won["outcome"] == "won_project"


# ==============================================================================
# 11. Outcome Analytics Metrics & Learning Safety
# ==============================================================================
def test_outcome_analytics_metrics_and_learning_safety():
    with db.connect() as conn:
        l1 = _create_lead(conn, email="l1@test.com", segment="gulf_realestate", opportunity_type="contract")
        m1 = _create_message(conn, l1, step=0)
        db.record_message_outcome(conn, message_id=m1, lead_id=l1, mode="freelance", outcome="delivered",
                                  opportunity_score=90, email_confidence=95, contact_role="CTO",
                                  lead_source="website", cta_type="one_page_plan", message_angle="speed")

        l2 = _create_lead(conn, email="l2@test.com", segment="gulf_realestate", opportunity_type="contract")
        m2 = _create_message(conn, l2, step=0)
        db.record_message_outcome(conn, message_id=m2, lead_id=l2, mode="freelance", outcome="positive_reply",
                                  opportunity_score=88, email_confidence=95, contact_role="Founder",
                                  lead_source="website", cta_type="one_page_plan", message_angle="speed")

        l3 = _create_lead(conn, email="l3@test.com", segment="india_startups_intern", opportunity_type="internship")
        m3 = _create_message(conn, l3, step=0)
        db.record_message_outcome(conn, message_id=m3, lead_id=l3, mode="internship", outcome="interview",
                                  opportunity_score=85, email_confidence=85, contact_role="Engineering Manager",
                                  lead_source="jobs_hn", cta_type="trial_scope", message_angle="growth")

        # 1. Calculate outcome metrics (NO OPEN RATE)
        metrics = analytics.calculate_outcome_metrics(conn)
        assert "open_rate" not in metrics
        assert metrics["total_sent"] == 3
        assert metrics["delivered"] == 3
        assert metrics["bounced"] == 0
        assert metrics["delivery_rate"] == 100.0
        assert metrics["bounce_rate"] == 0.0

        # Mode comparison
        comp = analytics.get_mode_comparison(conn)
        assert comp["freelance"]["total_sent"] == 2
        assert comp["internship"]["total_sent"] == 1
        assert comp["internship"]["interview"] == 1

        # Priority comparison
        prios = analytics.get_priority_comparison(conn)
        assert len(prios) > 0

        # 2. Learning Safety: Recommendations propose changes with disclaimer and NEVER modify weights automatically
        recs = analytics.get_recommendations(conn)
        for r in recs:
            assert "action_required" in r
            # Confirms safety rule: automated learning advises, never overwrites automatically
            assert "Manual" in r["action_required"] or "None" in r["action_required"]
