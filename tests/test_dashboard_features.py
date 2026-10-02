"""Tests for enhanced dashboard views and controls: Schedule, Outcomes, Doctor, and Quality Gates."""
import pytest
from outreach import db, dashboard_sync
from dashboard.api import index as dash_api


def test_dashboard_schedule_items():
    with db.connect() as conn:
        lead_id = db.add_lead(
            conn,
            company="Horizon AI",
            domain="horizonai.com",
            email="sarah@horizonai.com",
            first_name="Sarah",
            segment="us_agencies",
            status="approved",
            email_status="valid",
            email_source="hunter",
        )
        conn.execute("UPDATE leads SET score=88, score_total=88 WHERE id=?", (lead_id,))
        conn.execute(
            """INSERT INTO messages (lead_id, step, subject, body, status, confidence)
               VALUES (?, 0, 'Quick question on your real estate pipeline',
                       'Hi Sarah, saw your recent AI integration and wanted to reach out.',
                       'approved', 0.85)""",
            (lead_id,)
        )
        conn.execute(
            """INSERT INTO messages (lead_id, step, subject, body, status, due_at)
               VALUES (?, 1, 'Follow up on real estate AI',
                       'Hi Sarah, following up on my previous note.',
                       'approved', '2026-10-05T10:00:00Z')""",
            (lead_id,)
        )
        conn.execute("UPDATE leads SET status='active' WHERE id=?", (lead_id,))

        sch = dashboard_sync._schedule_items(conn)
        assert "windows" in sch
        assert len(sch["windows"]) > 0
        assert "upcoming_firsts" in sch
        assert "upcoming_followups" in sch
        assert len(sch["upcoming_followups"]) >= 1
        assert "allocation" in sch
        assert sch["allocation"]["daily_new_limit"] == 28


def test_dashboard_outcomes_summary():
    with db.connect() as conn:
        lead_id = db.add_lead(
            conn,
            company="Apex Labs",
            domain="apexlabs.com",
            email="tech@apexlabs.com",
            first_name="Alex",
            segment="intl_freelance_posts",
            status="active",
        )
        conn.execute("UPDATE leads SET score=82, score_total=82 WHERE id=?", (lead_id,))
        cur = conn.execute(
            """INSERT INTO messages (lead_id, step, subject, body, status, sent_at)
               VALUES (?, 0, 'Technical implementation outline',
                       'Hi Alex, I can outline how I would implement this backend worker.',
                       'sent', '2026-10-01T12:00:00Z')""",
            (lead_id,)
        )
        msg_id = cur.lastrowid
        db.record_message_outcome(
            conn,
            message_id=msg_id,
            lead_id=lead_id,
            mode="freelance",
            opportunity_score=82,
            outcome="meeting",
            notes="Booked discovery call",
            cta_type="technical_proposal",
            message_angle="technical_depth",
            lead_source="hn_freelance",
            contact_role="CTO",
            email_confidence=95,
        )

        outcomes = dashboard_sync._outcomes_summary(conn)
        assert "overall" in outcomes
        assert "freelance" in outcomes
        assert "internship" in outcomes
        assert "sources" in outcomes
        assert "ctas" in outcomes
        assert "roles" in outcomes
        assert "recommendations" in outcomes
        assert "recent" in outcomes
        assert len(outcomes["recent"]) >= 1
        assert outcomes["recent"][0]["outcome"] == "meeting"


def test_dashboard_doctor_summary():
    doc = dashboard_sync._doctor_summary()
    assert "subsystems" in doc
    assert "environment" in doc["subsystems"]
    assert "database" in doc["subsystems"]
    assert "schema" in doc["subsystems"]
    assert "quota" in doc["subsystems"]


def test_dashboard_action_record_outcome():
    with db.connect() as conn:
        lead_id = db.add_lead(
            conn,
            company="Omni Corp",
            domain="omni.com",
            email="mark@omni.com",
            first_name="Mark",
            segment="uk_agencies",
            status="active",
        )
        conn.execute("UPDATE leads SET score=80, score_total=80 WHERE id=?", (lead_id,))
        cur = conn.execute(
            """INSERT INTO messages (lead_id, step, subject, body, status)
               VALUES (?, 0, 'Workflow audit', 'Hi Mark, noticing your agency processes.', 'sent')""",
            (lead_id,)
        )
        msg_id = cur.lastrowid

    res = dashboard_sync._record_outcome(msg_id, {
        "outcome": "positive_reply",
        "notes": "Client requested call on Monday",
        "stage": "call_booked",
    })
    assert "recorded outcome: positive_reply" in res

    with db.connect() as conn:
        row = conn.execute("SELECT outcome, notes FROM message_outcomes WHERE message_id=?", (msg_id,)).fetchone()
        assert row is not None
        assert row[0] == "positive_reply"
        assert "Client requested call" in row[1]
        lead_row = conn.execute("SELECT deal_stage FROM leads WHERE id=?", (lead_id,)).fetchone()
        assert lead_row[0] == "call_booked"


def test_dashboard_action_trigger_tick():
    res = dashboard_sync._trigger_tick(1, {"dry_run": True, "max_sends": 2})
    assert "simulated" in res


def test_dashboard_api_clean_action_validation():
    # Valid record_outcome
    kind, target, payload = dash_api.clean_action({
        "kind": "record_outcome",
        "target": 101,
        "payload": {
            "outcome": "meeting",
            "notes": "Booked call",
            "stage": "call_booked"
        }
    })
    assert kind == "record_outcome"
    assert target == 101
    assert payload["outcome"] == "meeting"
    assert payload["stage"] == "call_booked"

    # Invalid outcome raises ValueError
    with pytest.raises(ValueError, match="unknown outcome"):
        dash_api.clean_action({
            "kind": "record_outcome",
            "target": 101,
            "payload": {"outcome": "invalid_outcome"}
        })

    # Valid trigger_tick
    kind, target, payload = dash_api.clean_action({
        "kind": "trigger_tick",
        "target": 1,
        "payload": {"dry_run": True, "max_sends": 1}
    })
    assert kind == "trigger_tick"
    assert payload["dry_run"] is True
    assert payload["max_sends"] == 1
