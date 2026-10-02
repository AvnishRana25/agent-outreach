"""Test suite for Phase 4: Production Hardening for Unattended GitHub Actions Operation.

Verifies:
1. Global kill switch fail-safe behavior
2. Production-safe dry-run execution (zero quota consumed, zero outbound messages)
3. Idempotency guarantees, atomic state transitions, and crash recovery
4. Transient vs permanent retry classification with exponential backoff
5. Bounce protection sentinels and automated pause triggers
6. 28 new emails/day ceiling enforcement with unconstrained follow-up dispatch
7. Structured logging, metrics capture, and secret masking
8. Non-destructive Health Doctor inspection
9. Central alerting with anti-spam cooldown protection
"""
from __future__ import annotations

import os
import sqlite3
import smtplib
import socket
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
import requests

from outreach import alerts, config, db, doctor, eligibility, logging, retries, sender, transport


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Set up an isolated test database and environment for each test."""
    test_db = str(tmp_path / "phase4_test.db")
    monkeypatch.setenv("OUTREACH_DB", test_db)
    monkeypatch.setenv("OUTREACH_ENV", "local")
    monkeypatch.setenv("OUTREACH_SENDING_ENABLED", "true")
    monkeypatch.setenv("OUTREACH_DRY_RUN", "false")
    db.init()

    monkeypatch.setattr(sender.config, "inboxes", lambda: [{"email": "me@zoho.in", "max_per_day": 35}])
    monkeypatch.setattr(sender, "in_window", lambda seg, now: True)
    monkeypatch.setattr(sender, "_signature", lambda seg: "Avnish Rana")
    monkeypatch.setattr(sender, "_choose_inbox", lambda *a: {"email": "me@zoho.in"})

    with db.connect() as conn:
        db.set_state(conn, "last_inbound_sync:me@zoho.in", datetime.now(timezone.utc).isoformat())


# ==============================================================================
# Helper functions
# ==============================================================================
def _create_lead(conn, email: str = "founder@startup.com", segment: str = "uk_agencies", opportunity_type: str = "contract", **kwargs) -> int:
    local_part = email.split("@")[0] if "@" in email else "lead"
    domain = kwargs.pop("domain", None) or f"{local_part}.com"
    defaults = dict(
        email=email,
        domain=domain,
        first_name="Alex",
        last_name="Smith",
        company=f"Company {local_part}",
        segment=segment,
        status="approved",
        email_status="valid",
        email_source="hunter",
        fit=8,
        score=85,
        score_total=85,
        opportunity_type=opportunity_type,
        verified_evidence='[{"fact": "Seeking AI workflow partner", "source": "https://example.com", "verified": true}]',
    )
    defaults.update(kwargs)
    db.add_lead(conn, **defaults)
    row = conn.execute("SELECT id FROM leads WHERE email=?", (defaults["email"],)).fetchone()
    if not row:
        raise ValueError(f"Failed to create lead with email {defaults['email']}")
    return row[0]


def _create_message(conn, lead_id: int, step: int = 0, status: str = "approved") -> int:
    idemp_key = f"lead:{lead_id}:step:{step}"
    cur = conn.execute(
        "INSERT INTO messages (lead_id, step, status, subject, body, confidence, approved_by, due_at, idempotency_key) "
        "VALUES (?, ?, ?, 'Quick question regarding AI automation', 'Saw your post and wanted to share our approach.', "
        "0.90, 'human_reviewer', '2026-01-01T00:00:00Z', ?) RETURNING id",
        (lead_id, step, status, idemp_key)
    )
    return cur.fetchone()[0]


# ==============================================================================
# 1. Global Kill Switch & Fail-Safe Behavior
# ==============================================================================
def test_kill_switch_blocks_outbound_sending(monkeypatch):
    monkeypatch.setenv("OUTREACH_SENDING_ENABLED", "false")
    monkeypatch.setenv("OUTREACH_DRY_RUN", "false")

    assert config.is_sending_enabled() is False

    with db.connect() as conn:
        lead_id = _create_lead(conn, email="killswitch@test.com")
        _create_message(conn, lead_id, step=0)

    # Sender tick must return 0 immediately and send nothing
    sent_count = sender.tick(max_sends=5, dry_run=False)
    assert sent_count == 0


def test_production_failsafe_defaults_to_disabled(monkeypatch):
    monkeypatch.setenv("OUTREACH_ENV", "production")
    monkeypatch.delenv("OUTREACH_SENDING_ENABLED", raising=False)

    # In production without explicit OUTREACH_SENDING_ENABLED=true, it MUST fail safe
    assert config.is_sending_enabled() is False

    monkeypatch.setenv("OUTREACH_SENDING_ENABLED", "invalid_value")
    assert config.is_sending_enabled() is False

    monkeypatch.setenv("OUTREACH_SENDING_ENABLED", "true")
    assert config.is_sending_enabled() is True


# ==============================================================================
# 2. Production-Safe Dry-Run
# ==============================================================================
def test_dry_run_consumes_zero_quota_and_sends_zero_emails(monkeypatch):
    monkeypatch.setattr(sender, "in_window", lambda *a: True)
    monkeypatch.setattr(sender, "inbox_fresh", lambda *a: True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="dryrun_target@test.com")
        msg_id = _create_message(conn, lead_id, step=0)

    # Mock transport.send so if it were called, it would raise an error
    send_mock = MagicMock(side_effect=RuntimeError("transport.send must NEVER be called in dry run"))
    monkeypatch.setattr(transport, "send", send_mock)

    sent = sender.tick(max_sends=1, dry_run=True)
    assert sent == 1

    # Transport was never called
    send_mock.assert_not_called()

    with db.connect() as conn:
        # Quota remains 0
        boxes = [b for b in config.inboxes() if b.get("enabled", True)]
        send_count_logged = sum(db.send_count(conn, today, b["email"]) for b in boxes)
        assert send_count_logged == 0

        # Message status remains approved (not marked sent)
        msg_row = conn.execute("SELECT status FROM messages WHERE id=?", (msg_id,)).fetchone()
        assert msg_row[0] == "approved"


# ==============================================================================
# 3. Idempotency, Duplicate Send Protection & Crash Recovery
# ==============================================================================
def test_idempotency_key_constraint(monkeypatch):
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="idemp@test.com")
        _create_message(conn, lead_id, step=0)

        # Attempting to insert another message with the same idempotency_key must fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (lead_id, step, status, idempotency_key) VALUES (?, 0, 'draft', ?)",
                (lead_id, f"lead:{lead_id}:step:0")
            )


def test_reconcile_stale_sending_recovers_crashed_in_flight():
    with db.connect() as conn:
        lead_id = _create_lead(conn, email="stale_crash@test.com")
        msg_id = _create_message(conn, lead_id, step=0, status="sending")

        # Set sending_at to 30 minutes ago
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        conn.execute("UPDATE messages SET sending_at=? WHERE id=?", (past_iso, msg_id))

        reconciled = sender.reconcile_stale_sending(conn, max_age_minutes=15)
        assert reconciled >= 1

        msg = conn.execute("SELECT status, error FROM messages WHERE id=?", (msg_id,)).fetchone()
        assert msg["status"] == "needs_reconciliation"
        assert "timed out" in msg["error"].lower() or "terminated" in msg["error"].lower()


def test_duplicate_send_prevention_in_sender_loop(monkeypatch):
    monkeypatch.setattr(sender, "in_window", lambda *a: True)
    monkeypatch.setattr(sender, "inbox_fresh", lambda *a: True)

    with db.connect() as conn:
        lead_id = _create_lead(conn, email="already_sent_lead@test.com")
        _create_message(conn, lead_id, step=0, status="sent")

        # 1. Database constraint: inserting another step 0 for the same lead MUST raise IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages (lead_id, step, status, subject, body, approved_by, idempotency_key) "
                "VALUES (?, 0, 'approved', 'Sub', 'Body', 'reviewer', 'lead-dup-test')",
                (lead_id,)
            )

        # 2. Gate H: If initial outreach was already sent for this lead, Gate H blocks sending
        lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        dummy_msg = {"id": 999, "lead_id": lead_id, "step": 0, "status": "approved", "confidence": 0.9}
        res = eligibility.evaluate_send_eligibility(lead, dummy_msg, conn)
        assert res.eligible is False
        assert "contact_history" in res.failed_gates
        assert "already sent" in res.reasons["contact_history"].reason.lower()


# ==============================================================================
# 4. Retries & Error Classification
# ==============================================================================
def test_retry_transient_failures():
    attempts = 0

    def flaky_func():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise requests.exceptions.Timeout("Connection timed out")
        return "success"

    result = retries.with_retry(flaky_func, max_retries=3, initial_delay=0.01, backoff_factor=1.5)
    assert result == "success"
    assert attempts == 3


def test_retry_permanent_failure_raised_immediately():
    attempts = 0

    def fail_hard():
        nonlocal attempts
        attempts += 1
        raise smtplib.SMTPRecipientsRefused({"test@invalid.com": (550, b"User unknown")})

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        retries.with_retry(fail_hard, max_retries=3, initial_delay=0.01)

    # Permanent failures must NEVER be retried
    assert attempts == 1


# ==============================================================================
# 5. Bounce Protection Sentinel
# ==============================================================================
def test_bounce_protection_pauses_inbox_and_alerts(monkeypatch):
    monkeypatch.setenv("MAX_BOUNCE_RATE", "0.05")
    monkeypatch.setenv("MIN_BOUNCE_SAMPLE", "20")

    alert_called = False
    def mock_alert(rate, total, threshold, inbox="global"):
        nonlocal alert_called
        alert_called = True
        return True

    monkeypatch.setattr(alerts, "alert_bounce_threshold_exceeded", mock_alert)
    # Mock bounce rate: 3 bounces out of 20 sends = 15% bounce rate (> 5%)
    monkeypatch.setattr(sender, "_bounce_rate", lambda conn, email: (3, 20))

    with db.connect() as conn:
        lead_id = _create_lead(conn, email="bounce_test@test.com")
        _create_message(conn, lead_id, step=0)

        sent = sender.tick(max_sends=1, dry_run=False)
        assert sent == 0
        assert alert_called is True


# ==============================================================================
# 6. 28 New Emails Ceiling & Unconstrained Follow-ups
# ==============================================================================
def test_28_new_emails_ceiling_blocks_first_touch_only(monkeypatch):
    monkeypatch.setattr(sender, "in_window", lambda *a: True)
    monkeypatch.setattr(sender, "inbox_fresh", lambda *a: True)
    monkeypatch.setenv("DAILY_NEW_LIMIT", "28")
    monkeypatch.setenv("DAILY_SEND_LIMIT", "28")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with db.connect() as conn:
        # Simulate 28 new first emails already sent today
        for _ in range(28):
            db.bump_send_count(conn, today, "workwithavnish@zohomail.in", "first")
            db.bump_send_count(conn, today, "workwithavnish@zohomail.in", "first_freelance")

        counts = db.get_daily_allocation_counts(conn, today)
        assert counts["freelance"] == 28

        # 1. New initial email (step == 0) should be blocked by Gate I
        lead_first = _create_lead(conn, email="new_touch_29@test.com")
        msg_first = _create_message(conn, lead_first, step=0)
        lead_row = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_first,)).fetchone())
        msg_row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_first,)).fetchone())

        res_first = eligibility.evaluate_send_eligibility(lead_row, msg_row, conn)
        assert res_first.eligible is False
        assert "quota" in res_first.failed_gates
        assert "Daily send ceiling reached" in res_first.reasons["quota"].reason or "Daily freelance" in res_first.reasons["quota"].reason

        # 2. Due follow-up (step == 1) for an active sequence MUST NOT be blocked by the 28 ceiling
        lead_fu = _create_lead(conn, email="active_sequence@test.com")
        db.set_lead(conn, lead_fu, status="active")
        # Record step 0 as already sent
        conn.execute("INSERT INTO messages (lead_id, step, status, subject, body, sent_at) VALUES (?, 0, 'sent', 'Sub', 'Body', CURRENT_TIMESTAMP)", (lead_fu,))
        msg_fu = _create_message(conn, lead_fu, step=1)
        lead_fu_row = dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_fu,)).fetchone())
        msg_fu_row = dict(conn.execute("SELECT * FROM messages WHERE id=?", (msg_fu,)).fetchone())

        res_fu = eligibility.evaluate_send_eligibility(lead_fu_row, msg_fu_row, conn)
        assert res_fu.reasons["quota"].passed is True
        assert "unconstrained" in res_fu.reasons["quota"].reason.lower()


# ==============================================================================
# 7. Structured Logging & Secret Masking
# ==============================================================================
def test_secret_masking_in_logs():
    sensitive_message = "Bearer secret_jwt_token_123456789 and api_key=AIzaSyA12345678901234567890123456789012"
    masked = logging.mask_secrets(sensitive_message)
    assert "secret_jwt_token" not in masked
    assert "AIzaSy" not in masked
    assert "***REDACTED***" in masked


def test_structured_log_event_emission(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "987654")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Scheduled Outreach")

    entry = logging.log_event(
        operation="test_op",
        result="success",
        lead_id=42,
        message_id=99,
        mode="freelance",
        duration_ms=45.2,
        extra_data="safe_content"
    )
    assert entry["operation"] == "test_op"
    assert entry["result"] == "success"
    assert entry["lead_id"] == 42
    assert entry["mode"] == "freelance"
    assert entry["duration_ms"] == 45.2
    assert entry["github_run_id"] == "987654"
    assert entry["github_workflow"] == "Scheduled Outreach"


# ==============================================================================
# 8. Health Doctor Non-Destructive Inspection
# ==============================================================================
def test_health_doctor_inspect_health_never_sends_emails(monkeypatch):
    # Ensure transport.send is not called
    transport_mock = MagicMock(side_effect=RuntimeError("Doctor must never send outreach"))
    monkeypatch.setattr(transport, "send", transport_mock)

    report = doctor.inspect_health()
    transport_mock.assert_not_called()

    # Subsystems verified
    sub = report["subsystems"]
    assert "environment" in sub
    assert "database" in sub
    assert "schema" in sub
    assert "llm" in sub
    assert "inboxes" in sub
    assert "kill_switch" in sub
    assert "dry_run" in sub
    assert "quota" in sub
    assert "stuck_messages" in sub

    assert sub["quota"]["daily_new_ceiling"] == 28
    assert sub["quota"]["followups_limit"] == "unconstrained"


# ==============================================================================
# 9. Alerting Anti-Spam Cooldown
# ==============================================================================
def test_alert_cooldown_prevents_spam(monkeypatch):
    post_mock = MagicMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr(requests, "post", post_mock)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "mock_bot_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "mock_chat_id")

    # Clear memory cooldown
    alerts._MEMORY_COOLDOWN.clear()

    # First alert should succeed
    sent1 = alerts.send_alert("test_key", "Critical issue detected", cooldown_minutes=15)
    assert sent1 is True
    assert post_mock.call_count == 1

    # Second immediate alert for same key should be suppressed by cooldown
    sent2 = alerts.send_alert("test_key", "Critical issue again", cooldown_minutes=15)
    assert sent2 is False
    assert post_mock.call_count == 1  # No additional HTTP request made
