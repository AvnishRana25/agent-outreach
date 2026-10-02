"""Tests for Phase 2: Enforced quality gates, opportunity scoring, contact verification,
and evidence-backed personalization.

Tests cover all 14 specified gate scenarios, candidate ranking, and CLI integration.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest import mock
import pytest

from outreach import config, db, eligibility, evidence, scoring, sender, verification


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Set up an isolated test database and environment for each test."""
    test_db = str(tmp_path / "quality_gates.db")
    monkeypatch.setenv("OUTREACH_DB", test_db)
    monkeypatch.setenv("OUTREACH_ENV", "local")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    db.init()

    # Default mocked inbox and sending window
    monkeypatch.setattr(sender.config, "inboxes", lambda: [{"email": "me@zoho.in", "max_per_day": 35}])
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana")
    monkeypatch.setattr(sender, "_choose_inbox", lambda *a: {"email": "me@zoho.in"})

    with db.connect() as conn:
        db.set_state(conn, "last_inbound_sync:me@zoho.in", datetime.now(timezone.utc).isoformat())


def _create_lead(conn, **kwargs) -> int:
    """Helper to create a lead in the test database and return its id."""
    created_at_override = kwargs.pop("created_at", None)
    kwargs.pop("created_utc", None)
    if "name" in kwargs:
        full_name = kwargs.pop("name")
        first, _, last = full_name.partition(" ")
        kwargs["first_name"] = first
        kwargs["last_name"] = last
    unique_id = abs(hash(str(kwargs)))
    email = kwargs.get("email", f"test_{unique_id}@example.com")
    domain = kwargs.get("domain", f"example_{unique_id}.com")
    defaults = dict(
        email=email,
        domain=domain,
        company="Example Corp",
        segment="intl_freelance_posts",
        status="approved",
        email_status="valid",
        email_source="prospeo",
        fit=9,
    )
    defaults.update(kwargs)
    db.add_lead(conn, **defaults)
    row = conn.execute("SELECT id FROM leads WHERE email=?", (defaults["email"],)).fetchone()
    if not row:
        raise ValueError(f"Failed to insert lead: {defaults}")
    lead_id = row[0]
    if created_at_override:
        conn.execute("UPDATE leads SET created_at=? WHERE id=?", (created_at_override, lead_id))
    return lead_id


def _create_message(conn, lead_id: int, **kwargs) -> int:
    """Helper to create a message for a lead and return its id."""
    lead_row = conn.execute("SELECT first_name FROM leads WHERE id=?", (lead_id,)).fetchone()
    first_name = (lead_row[0] if lead_row and lead_row[0] else "there").strip()
    defaults = dict(
        lead_id=lead_id,
        step=0,
        subject="Collaboration idea",
        body=f"Hi {first_name}, noticed your project and would love to help.",
        status="approved",
        confidence=0.85,
        approved_by="avnish",
        idempotency_key=f"lead:{lead_id}:step:{kwargs.get('step', 0)}",
    )
    defaults.update(kwargs)
    cols = ", ".join(defaults.keys())
    placeholders = ", ".join(["?"] * len(defaults))
    conn.execute(f"INSERT INTO messages ({cols}) VALUES ({placeholders})", tuple(defaults.values()))
    row = conn.execute("SELECT id FROM messages WHERE lead_id=? AND step=?", (lead_id, defaults["step"])).fetchone()
    return row[0]


# ==============================================================================
# 1. SCENARIO 1: Strong Freelance Candidate Passes Eligibility
# ==============================================================================
def test_gate_scenario_1_strong_freelance_passes():
    """A strong freelance opportunity with explicit intent, tech fit, verified email,
    relevant CTO/founder contact, and verified evidence passes all Gates A-J."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="sarah@pythonflows.io",
            domain="pythonflows.io",
            company="PythonFlows",
            segment="intl_freelance_posts",
            name="Sarah Connor",
            title="Co-founder & CTO",
            source_text="[Hiring] Looking for a senior Python backend developer / freelancer to build FastAPI automation scrapers and PostgreSQL pipeline. Budget $3,500.",
            site_text="PythonFlows automates data extraction pipelines using modern Python.",
            created_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            verified_evidence=json.dumps([
                {"fact": "FastAPI and PostgreSQL pipeline", "fact_type": "tech_stack", "confidence": 0.95, "status": "VERIFIED_FACT"},
                {"fact": "Build automation scrapers for data extraction", "fact_type": "hiring_need", "confidence": 0.9, "status": "VERIFIED_FACT"}
            ]),
            email_verification_detail=json.dumps({
                "email": "sarah@pythonflows.io",
                "status": "valid",
                "confidence_score": 95,
                "confidence_level": "verified",
                "is_catch_all": False,
                "provider": "prospeo",
            })
        )
        msg_id = _create_message(
            conn,
            lead_id,
            step=0,
            confidence=0.90,
            status="approved",
            approved_by="avnish",
        )

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        # Verify opportunity scoring
        opp_score = scoring.score_lead(lead, conn=conn)
        assert opp_score.score_total >= 75
        assert opp_score.score_band in ("Priority A", "Priority B")
        assert opp_score.components["intent"] >= 80
        assert opp_score.components["technical_fit"] >= 80

        # Evaluate send eligibility
        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is True
        assert len(result.failed_gates) == 0
        assert "opportunity_score" in result.passed_gates
        assert "email_verification" in result.passed_gates
        assert "contact_relevance" in result.passed_gates
        assert "evidence_quality" in result.passed_gates
        assert "approval" in result.passed_gates


# ==============================================================================
# 2. SCENARIO 2: Strong Internship Candidate Passes Eligibility
# ==============================================================================
def test_gate_scenario_2_strong_internship_passes():
    """A strong internship opportunity with explicit internship intent, matching tech stack,
    relevant recruiter/engineering manager role, and verified evidence passes all Gates A-J."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="recruiting@deepai.org",
            domain="deepai.org",
            company="DeepAI Labs",
            segment="intl_startups_intern",
            name="Marcus Vance",
            title="Engineering Manager",
            source_text="We are hiring a Machine Learning Intern for summer 2026. Focus on Python, PyTorch fine-tuning and LLM agent evaluation.",
            created_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            verified_evidence=json.dumps([
                {"fact": "Machine Learning Intern for summer 2026", "fact_type": "hiring_need", "confidence": 0.95, "status": "VERIFIED_FACT"},
                {"fact": "PyTorch fine-tuning and LLM agent evaluation", "fact_type": "tech_stack", "confidence": 0.9, "status": "VERIFIED_FACT"}
            ]),
            email_verification_detail=json.dumps({
                "email": "recruiting@deepai.org",
                "status": "valid",
                "confidence_score": 90,
                "confidence_level": "verified",
                "is_catch_all": False,
                "provider": "prospeo",
            })
        )
        msg_id = _create_message(
            conn,
            lead_id,
            step=0,
            confidence=0.88,
            status="approved",
            approved_by="avnish",
        )

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        opp_score = scoring.score_lead(lead, conn=conn)
        assert opp_score.score_total >= 75
        assert opp_score.score_band in ("Priority A", "Priority B")

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is True
        assert len(result.failed_gates) == 0


# ==============================================================================
# 3. SCENARIO 3: Weak Opportunity Score (< 75) Blocked by Gate A
# ==============================================================================
def test_gate_scenario_3_weak_opportunity_blocked_by_gate_a():
    """An opportunity scoring < 75 is blocked by Gate A, even if approved."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="vague@generic.com",
            domain="generic.com",
            company="Generic Co",
            segment="intl_freelance_posts",
            name="Bob",
            title="Accountant",  # Irrelevant role
            source_text="Thinking about maybe building a mobile app someday in PHP or Flutter.",
            fit=4,
        )
        msg_id = _create_message(
            conn,
            lead_id,
            step=0,
            confidence=0.80,
            status="approved",
            approved_by="avnish",
        )

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        opp_score = scoring.score_lead(lead, conn=conn)
        assert opp_score.score_total < 75
        assert opp_score.score_band in ("Manual Review", "Reject")

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "opportunity_score" in result.failed_gates
        assert "below required threshold >=75" in result.reasons["opportunity_score"].reason


# ==============================================================================
# 4. SCENARIO 4: Stale Opportunity (> 30 days) Penalized / Blocked
# ==============================================================================
def test_gate_scenario_4_stale_opportunity_penalized_and_blocked():
    """Opportunities older than 30 days receive low recency score and get blocked by Gate A."""
    now = datetime.now(timezone.utc)

    # 1. Test recency function decay explicitly
    rec_fresh = scoring.calculate_recency({"source_text": f"Posted {(now - timedelta(days=1)).strftime('%Y-%m-%d')}"})
    rec_10d = scoring.calculate_recency({"source_text": f"Posted {(now - timedelta(days=10)).strftime('%Y-%m-%d')}"})
    rec_20d = scoring.calculate_recency({"source_text": f"Posted {(now - timedelta(days=20)).strftime('%Y-%m-%d')}"})
    rec_stale = scoring.calculate_recency({"source_text": f"Posted {(now - timedelta(days=35)).strftime('%Y-%m-%d')}"})

    assert rec_fresh[0] == 100.0
    assert rec_10d[0] == 70.0
    assert rec_20d[0] == 40.0
    assert rec_stale[0] == 15.0
    assert "stale" in rec_stale[1].lower()

    # 2. Test in eligibility evaluation
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="oldpost@legacy.org",
            domain="legacy.org",
            company="Legacy Org",
            segment="intl_freelance_posts",
            name="Old Poster",
            title="Founder",
            source_text=f"Hiring developer for python automation scripts (Posted {(now - timedelta(days=45)).strftime('%Y-%m-%d')}).",
            created_at=(now - timedelta(days=45)).isoformat(),
        )
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        opp_score = scoring.score_lead(lead, conn=conn)
        assert opp_score.components["recency"] <= 20.0
        assert opp_score.score_total < 75

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "opportunity_score" in result.failed_gates


# ==============================================================================
# 5. SCENARIO 5: Negative Intent Post Rejected with Intent Score 0
# ==============================================================================
@pytest.mark.parametrize("neg_text", [
    "We are not hiring any freelancers or contractors right now.",
    "Strictly NO AGENCIES or recruiters please.",
    "Position is closed. Do not contact us.",
    "Not looking for agency help or outsourcing.",
    "Hiring freeze in effect across all engineering departments.",
])
def test_gate_scenario_5_negative_intent_post_rejected(neg_text):
    """Negative intent patterns immediately return intent score 0, failing Gate A."""
    score, reason = scoring.detect_intent({"source_text": neg_text})
    assert score == 0.0
    assert "negative intent detected" in reason.lower()

    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email=f"nohire_{abs(hash(neg_text))}@company.com",
            domain=f"company_{abs(hash(neg_text))}.com",
            company="NoHire Corp",
            source_text=neg_text,
        )
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        opp_score = scoring.score_lead(lead, conn=conn)
        assert opp_score.components["intent"] == 0.0

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "opportunity_score" in result.failed_gates


# ==============================================================================
# 6. SCENARIO 6: Unverified Email (< 80 confidence) Blocked by Gate B
# ==============================================================================
def test_gate_scenario_6_unverified_email_blocked_by_gate_b():
    """An email with confidence < 80 is blocked by Gate B."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="risky@unverified.io",
            domain="unverified.io",
            company="Risky Co",
            email_status="risky",
            email_verification_detail=json.dumps({
                "email": "risky@unverified.io",
                "status": "risky",
                "confidence_score": 65,
                "confidence_level": "risky",
                "is_catch_all": False,
                "provider": "hunter",
            })
        )
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "email_verification" in result.failed_gates


# ==============================================================================
# 7. SCENARIO 7: Catch-All Email Penalized and Blocked by Gate B
# ==============================================================================
def test_gate_scenario_7_catch_all_email_penalized_and_blocked_by_gate_b():
    """A catch-all email mailbox is penalized (-25) and fails Gate B unless strongly verified."""
    detail = verification.VerificationDetail(
        status="valid",
        confidence=75,
        is_catch_all=True,
        provider="prospeo"
    )
    # Penalized confidence drops by 25: 75 -> 50
    eff_conf = detail.effective_confidence()
    assert eff_conf == 50

    passed, reason = verification.evaluate_email_verification(detail, min_confidence=80)
    assert passed is False
    assert "Catch-all mailbox without strong verification" in reason

    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="team@catchall.com",
            domain="catchall.com",
            company="CatchAll Co",
            email_verification_detail=json.dumps({
                "email": "team@catchall.com",
                "status": "valid",
                "confidence_score": 75,
                "is_catch_all": True,
                "provider": "prospeo",
            })
        )
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "email_verification" in result.failed_gates


# ==============================================================================
# 8. SCENARIO 8: Suppressed Recipient Blocked Even with Human Approval / Override
# ==============================================================================
def test_gate_scenario_8_suppressed_recipient_blocked_even_with_approval():
    """Gate G (Suppression) is a hard gate: cannot be bypassed by human approval or override."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="optout@suppressed.org",
            domain="suppressed.org",
            company="Suppressed Org",
            status="unsubscribed",  # Unsubscribed lead
        )
        # Message marked approved with override=1
        msg_id = _create_message(
            conn,
            lead_id,
            step=0,
            status="approved",
            approved_by="avnish",
            override=1
        )

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "suppression" in result.failed_gates

    # Test explicit database suppression table entry
    with db.connect() as conn:
        lead_id2 = _create_lead(conn, email="do-not-email@acme.com", domain="acme.com", company="Acme")
        db.suppress(conn, "do-not-email@acme.com", "Manual opt-out")
        msg_id2 = _create_message(conn, lead_id2, step=0, status="approved", approved_by="admin", override=1)

        lead2 = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id2,)).fetchone())
        msg2 = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id2,)).fetchone())

        result2 = eligibility.evaluate_send_eligibility(lead2, msg2, conn)
        assert result2.eligible is False
        assert "suppression" in result2.failed_gates


# ==============================================================================
# 9. SCENARIO 9: Duplicate Contact History Blocked by Gate H
# ==============================================================================
def test_gate_scenario_9_duplicate_contact_history_blocked_by_gate_h():
    """Gate H (Contact History) is a hard gate: duplicate initial outreach cannot be sent."""
    # Case A: Initial message was already sent for this lead
    with db.connect() as conn:
        l1 = _create_lead(conn, email="sent@company.com", domain="company.com", company="Company 1")
        m1 = _create_message(conn, l1, step=0, status="sent")

        lead1 = dict(conn.execute("SELECT * FROM leads WHERE id=?", (l1,)).fetchone())
        msg1 = dict(conn.execute("SELECT * FROM messages WHERE id=?", (m1,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead1, msg1, conn)
        assert result.eligible is False
        assert "contact_history" in result.failed_gates
        assert "already sent" in result.reasons["contact_history"].reason.lower()

    # Case B: Lead sequence is already active
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="active@firm.com", domain="firm.com", company="Firm", status="active")
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="admin", override=1)
        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        result_active = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result_active.eligible is False
        assert "contact_history" in result_active.failed_gates
        assert "sequence is already active" in result_active.reasons["contact_history"].reason.lower()


# ==============================================================================
# 10. SCENARIO 10: Insufficient Verified Facts Blocked by Gate D
# ==============================================================================
def test_gate_scenario_10_insufficient_verified_facts_blocked_by_gate_d():
    """At least 1 verified personalization fact is required before first send."""
    with db.connect() as conn:
        # Lead has unverified notes with 0 verified facts
        lead_id = _create_lead(
            conn,
            email="nofacts@empty.com",
            domain="empty.com",
            company="Empty Co",
            verified_evidence=json.dumps([]),  # No verified facts
            notes="Rumors from an unverified blog post.",
        )
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "evidence_quality" in result.failed_gates
        assert "Insufficient verified personalization facts: 0" in result.reasons["evidence_quality"].reason


# ==============================================================================
# 11. SCENARIO 11: Low Personalization Confidence Blocked by Gate E
# ==============================================================================
def test_gate_scenario_11_low_confidence_or_hallucinated_claim_blocked_by_gate_e():
    """Drafts with personalization confidence < 0.75 without human approval fail Gate E."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="lowconf@test.com",
            domain="testconf.com",
            company="Low Conf",
            verified_evidence=json.dumps([
                {"fact": "React frontend", "fact_type": "tech_stack", "confidence": 0.9, "status": "VERIFIED_FACT"}
            ])
        )
        # Message has confidence 0.60 and is not approved by human
        msg_id = _create_message(
            conn,
            lead_id,
            step=0,
            confidence=0.60,
            status="draft",
            approved_by=""
        )

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert result.eligible is False
        assert "personalization_confidence" in result.failed_gates
        assert "Personalization confidence 0.60 below required threshold >=0.75" in result.reasons["personalization_confidence"].reason

    # Anti-hallucination validation check
    valid_ev = [
        evidence.EvidenceRecord(
            fact="Uses Python and FastAPI",
            fact_type=evidence.FactType.TECH_STACK,
            source_type="github",
            source_url="https://github.com/test",
            verification_status=evidence.VerificationStatus.VERIFIED_FACT,
            confidence=0.9
        )
    ]
    is_valid, reason = evidence.validate_personalization_against_evidence(
        body="I saw your recent $50M funding on TechCrunch.",
        subject="Congrats on funding",
        evidence=valid_ev
    )
    assert is_valid is False
    assert "Ungrounded metric claim" in reason


# ==============================================================================
# 12. SCENARIO 12: Unapproved Initial Outreach Blocked by Gate F
# ==============================================================================
def test_gate_scenario_12_unapproved_initial_outreach_blocked_by_gate_f():
    """Unapproved initial outreach and auto-approved cold outreach are blocked by Gate F."""
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="draft@pending.com", domain="pending.com", company="Pending Co")

        # 1. Draft status blocked
        msg_id_draft = _create_message(conn, lead_id, step=0, status="draft", approved_by="")
        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg_draft = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id_draft,)).fetchone())

        result = eligibility.evaluate_send_eligibility(lead, msg_draft, conn)
        assert result.eligible is False
        assert "approval" in result.failed_gates
        assert "requires explicit human approval" in result.reasons["approval"].reason

        # 2. Auto-approved status blocked (cold initial outreach cannot bypass human review)
        conn.execute("UPDATE messages SET status='approved', approved_by='auto' WHERE id=?", (msg_id_draft,))
        msg_auto = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id_draft,)).fetchone())
        result_auto = eligibility.evaluate_send_eligibility(lead, msg_auto, conn)
        assert result_auto.eligible is False
        assert "approval" in result_auto.failed_gates
        assert "auto-approval forbidden" in result_auto.reasons["approval"].reason


# ==============================================================================
# 13. SCENARIO 13: Daily Send Limit Enforced at 28
# ==============================================================================
def test_gate_scenario_13_daily_send_limit_enforced_at_28(monkeypatch):
    """Daily send ceiling (28) is strictly enforced: 29th email is blocked by Gate I."""
    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="candidate29@target.com",
            domain="target.com",
            company="Target Corp",
            verified_evidence=json.dumps([
                {"fact": "Python automation", "fact_type": "tech_stack", "confidence": 0.9, "status": "VERIFIED_FACT"}
            ])
        )
        msg_id = _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")
        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone())

        # Mock db.send_count to simulate 27 sent today -> 28th passes
        monkeypatch.setattr(db, "send_count", lambda conn, today, box, kind="first": 27 if kind == "first" else 0)
        res_28 = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert "quota" in res_28.passed_gates

        # Mock db.send_count to simulate 28 sent today -> 29th fails Gate I
        monkeypatch.setattr(db, "send_count", lambda conn, today, box, kind="first": 28 if kind == "first" else 0)
        res_29 = eligibility.evaluate_send_eligibility(lead, msg, conn)
        assert res_29.eligible is False
        assert "quota" in res_29.failed_gates
        assert "Daily send ceiling reached: 28/28" in res_29.reasons["quota"].reason


# ==============================================================================
# 14. SCENARIO 14: Follow-up Gates Enforced
# ==============================================================================
def test_gate_scenario_14_followup_gates():
    """Follow-up policy requires: previous step sent, no reply, unsuppressed, due date elapsed, active."""
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="followup@lead.com", domain="lead.com", company="Followup Co", status="active")

        # Step 0 has status 'approved' (not 'sent' yet)
        _create_message(conn, lead_id, step=0, status="approved")
        # Step 1 scheduled
        msg_id1 = _create_message(
            conn,
            lead_id,
            step=1,
            status="pending",
            due_at=(now - timedelta(days=1)).isoformat()
        )

        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg1 = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id1,)).fetchone())

        # 1. Blocked because step 0 was not sent
        res = eligibility.evaluate_send_eligibility(lead, msg1, conn, now=now)
        assert res.eligible is False
        assert "previous_step" in res.failed_gates

        # Now mark step 0 as sent
        conn.execute("UPDATE messages SET status='sent' WHERE lead_id=? AND step=0", (lead_id,))

        # 2. Blocked if reply received
        conn.execute(
            "INSERT INTO replies (lead_id, inbox, received_at, subject, body, category) "
            "VALUES (?, 'me@zoho.in', ?, 'Re: test', 'Interested!', 'positive')",
            (lead_id, now.isoformat())
        )
        res_replied = eligibility.evaluate_send_eligibility(lead, msg1, conn, now=now)
        assert res_replied.eligible is False
        assert "no_reply" in res_replied.failed_gates

        # Remove reply
        conn.execute("DELETE FROM replies WHERE lead_id=?", (lead_id,))

        # 3. Blocked if lead unsubscribed / bounced
        conn.execute("UPDATE leads SET status='unsubscribed' WHERE id=?", (lead_id,))
        lead_unsub = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        res_unsub = eligibility.evaluate_send_eligibility(lead_unsub, msg1, conn, now=now)
        assert res_unsub.eligible is False
        assert "suppression" in res_unsub.failed_gates

        # 4. Blocked if due date not elapsed
        conn.execute("UPDATE leads SET status='active' WHERE id=?", (lead_id,))
        conn.execute("UPDATE messages SET due_at=? WHERE id=?", ((now + timedelta(days=2)).isoformat(), msg_id1))
        lead_active = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        msg_future = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id1,)).fetchone())
        res_future = eligibility.evaluate_send_eligibility(lead_active, msg_future, conn, now=now)
        assert res_future.eligible is False
        assert "timing_delay" in res_future.failed_gates

        # 5. Eligible when all follow-up criteria pass
        conn.execute("UPDATE messages SET due_at=? WHERE id=?", ((now - timedelta(hours=1)).isoformat(), msg_id1))
        msg_due = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_id1,)).fetchone())
        res_ok = eligibility.evaluate_send_eligibility(lead_active, msg_due, conn, now=now)
        assert res_ok.eligible is True
        assert len(res_ok.failed_gates) == 0


# ==============================================================================
# 15. Candidate Ranking in sender._candidates()
# ==============================================================================
def test_candidate_ranking_prioritizes_priority_a_and_higher_scores():
    """sender._candidates() sorts Priority A before Priority B, then by score_total descending."""
    with db.connect() as conn:
        # Lead 1: Priority B (score 78)
        l1 = _create_lead(
            conn,
            email="b1@domain1.com",
            domain="domain1.com",
            company="B Co 1",
            score_total=78,
            status="approved",
            email_status="valid",
            email_source="prospeo",
            created_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        )
        _create_message(conn, l1, step=0, status="approved", approved_by="avnish")

        # Lead 2: Priority A (score 92)
        l2 = _create_lead(
            conn,
            email="a1@domain2.com",
            domain="domain2.com",
            company="A Co 1",
            score_total=92,
            status="approved",
            email_status="valid",
            email_source="prospeo",
            created_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        _create_message(conn, l2, step=0, status="approved", approved_by="avnish")

        # Lead 3: Priority B (score 82)
        l3 = _create_lead(
            conn,
            email="b2@domain3.com",
            domain="domain3.com",
            company="B Co 2",
            score_total=82,
            status="approved",
            email_status="valid",
            email_source="prospeo",
            created_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        _create_message(conn, l3, step=0, status="approved", approved_by="avnish")

        candidates = sender._candidates(conn, datetime.now(timezone.utc).isoformat())
        emails = [c["email"] for c in candidates]

        # Order must be: Priority A (l2: 92) -> Priority B higher (l3: 82) -> Priority B lower (l1: 78)
        assert emails == ["a1@domain2.com", "b2@domain3.com", "b1@domain1.com"]


# ==============================================================================
# 16. CLI check-eligibility Integration
# ==============================================================================
def test_cli_check_eligibility(capsys):
    """The check-eligibility CLI command executes and reports gate breakdown cleanly."""
    from outreach import cli

    with db.connect() as conn:
        lead_id = _create_lead(
            conn,
            email="cli_test@gate.com",
            domain="gate.com",
            company="CLI Gate Corp",
            status="approved",
            email_status="valid",
            email_source="prospeo",
            score_total=88,
            score_reason_summary="Strong technical fit and verified CTO",
        )
        _create_message(conn, lead_id, step=0, status="approved", approved_by="avnish")

    # Run check-eligibility for this lead
    cli.main(["check-eligibility", "--lead-id", str(lead_id)])
    captured = capsys.readouterr()

    assert "Lead #" in captured.out
    assert "CLI Gate Corp" in captured.out
    assert "GATE A — Opportunity Quality" in captured.out
    assert "GATE B — Verified Contact" in captured.out
    assert "GATE F — Human Approval" in captured.out
    assert "GATE I — Daily Quota" in captured.out
