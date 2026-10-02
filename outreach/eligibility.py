"""Central send eligibility gatekeeper.

Evaluates candidate messages and prospects before ANY send (first contact or follow-up).
No first contact is allowed unless ALL required gates pass.
Enforces the 28 emails/day ceiling without ever lowering quality thresholds.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
from typing import Any
from pydantic import BaseModel, Field

from . import config, db
from .evidence import extract_evidence_from_lead, filter_verified_facts, count_verified_facts
from .verification import VerificationDetail, get_lead_verification_detail, evaluate_email_verification
from .scoring import OpportunityScore, score_lead, detect_intent

TEST_DRAFT_PATTERNS = re.compile(
    r"(?i:\b(?:a real draft|mock draft|placeholder draft|test draft|dummy draft)\b|\[mock:)",
)


def validate_message_content(lead: dict | Any, msg: dict | Any) -> tuple[bool, str]:
    """Deep check on message text to ensure no dummy text, placeholders, test artifacts, or name mismatches ever get sent."""
    lead = dict(lead) if lead is not None else {}
    msg = dict(msg) if msg is not None else {}
    body = (msg.get("body") or "").strip()
    subject = (msg.get("subject") or "").strip()

    if not body or not subject:
        return False, "Subject or body is empty"

    if db.MOCK_MARK in body or subject.startswith("[mock"):
        return False, "Message contains mock marker [mock:"

    if TEST_DRAFT_PATTERNS.search(body) or TEST_DRAFT_PATTERNS.search(subject):
        return False, "Message contains test draft pattern (e.g. 'a real draft')"

    if config.PLACEHOLDER.search(body) or config.PLACEHOLDER.search(subject):
        return False, "Message contains unfilled template placeholder"

    # Recipient first-name mismatch check
    first_name = (lead.get("first_name") if isinstance(lead, dict) else getattr(lead, "first_name", "")) or ""
    first_name = first_name.strip()
    if first_name:
        m = re.match(r"(?i)^(?:hi|hey|hello|dear)\s+([A-Za-z]+)", body)
        if m:
            greeting_name = m.group(1).strip()
            # Allow case-insensitive prefix match or exact match
            if not (
                greeting_name.lower() == first_name.lower()
                or first_name.lower().startswith(greeting_name.lower())
                or greeting_name.lower().startswith(first_name.lower())
            ):
                return False, f"Greeting name '{greeting_name}' does not match recipient first name '{first_name}'"

    return True, "Message content validated"


class GateResult(BaseModel):
    name: str
    passed: bool
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class EligibilityResult(BaseModel):
    eligible: bool
    summary: str
    failed_gates: list[str] = Field(default_factory=list)
    passed_gates: list[str] = Field(default_factory=list)
    reasons: dict[str, GateResult] = Field(default_factory=dict)
    score_total: int = 0
    score_band: str = "Reject"
    is_followup: bool = False


def _get_quality_config() -> dict[str, Any]:
    """Retrieve quality thresholds with robust defaults."""
    s = config.settings()
    q = s.get("quality", {})
    t = s.get("targeting", {})
    return {
        "min_opportunity_score": int(q.get("min_opportunity_score") or t.get("min_opportunity_score") or 75),
        "min_email_confidence": int(q.get("min_email_confidence") or t.get("min_email_confidence") or 80),
        "min_verified_personalization_facts": int(q.get("min_verified_personalization_facts") or 1),
        "min_personalization_confidence": float(q.get("min_personalization_confidence") or 0.75),
        "daily_send_ceiling": int(q.get("daily_send_ceiling") or s.get("sending", {}).get("daily_first_target") or 28),
    }


def in_window(seg: dict, now_utc: datetime) -> bool:
    """Check if the given time falls within the segment's sending days and windows."""
    tz = ZoneInfo(seg.get("timezone", "Asia/Kolkata"))
    local = now_utc.astimezone(tz)
    if local.isoweekday() not in seg.get("send_days", [1, 2, 3, 4, 5]):
        return False
    for window in seg.get("send_windows", ["09:45-12:30", "14:30-17:30"]):
        a, b = window.split("-")
        if time.fromisoformat(a) <= local.time() <= time.fromisoformat(b):
            return True
    return False


def evaluate_send_eligibility(
    lead: dict | Any,
    msg: dict | Any,
    conn: Any,
    now: datetime | None = None
) -> EligibilityResult:
    """Authoritative gate evaluation before sending ANY email (first touch or follow-up)."""
    lead = dict(lead) if lead is not None else {}
    msg = dict(msg) if msg is not None else {}
    now = now or datetime.now(timezone.utc)
    qc = _get_quality_config()
    step = int(msg.get("step") or 0)

    if step > 0:
        return _evaluate_followup_eligibility(lead, msg, conn, now, qc)
    else:
        return _evaluate_first_contact_eligibility(lead, msg, conn, now, qc)


def _evaluate_first_contact_eligibility(
    lead: dict | Any,
    msg: dict | Any,
    conn: Any,
    now: datetime,
    qc: dict[str, Any]
) -> EligibilityResult:
    """Evaluate Gates A through J for initial outreach."""
    lead = dict(lead) if lead is not None else {}
    msg = dict(msg) if msg is not None else {}
    gates: dict[str, GateResult] = {}
    failed: list[str] = []
    passed: list[str] = []

    # 0. GATE: Content Integrity (Hard gate: must check message text for placeholders, mock patterns, name mismatch)
    valid_content, content_reason = validate_message_content(lead, msg)
    if not valid_content:
        gates["content_integrity"] = GateResult(name="Content Integrity", passed=False, reason=content_reason)
        failed.append("content_integrity")
    else:
        gates["content_integrity"] = GateResult(name="Content Integrity", passed=True, reason="Message text passed content integrity check")
        passed.append("content_integrity")

    # 1. GATE G — Suppression (Hard gate: must check first, cannot be bypassed)
    email = lead.get("email") or ""
    domain = lead.get("domain") or ""
    lead_status = lead.get("status") or ""
    lead_id = lead.get("id")

    is_suppressed = False
    suppress_reason = ""

    if db.suppressed(conn, email):
        is_suppressed = True
        suppress_reason = f"Address {email} is on suppression list"
    elif domain and db.suppressed(conn, f"@{domain}"):
        is_suppressed = True
        suppress_reason = f"Domain @{domain} is on suppression list"
    elif lead_status in ("unsubscribed", "bounced", "rejected"):
        is_suppressed = True
        suppress_reason = f"Lead status is '{lead_status}'"
    else:
        # Check replies table for negative/bounce replies
        neg_reply = conn.execute(
            "SELECT category FROM replies WHERE lead_id=? AND category IN ('bounce','unsubscribe','not_interested') LIMIT 1",
            (lead_id,)
        ).fetchone()
        if neg_reply:
            is_suppressed = True
            suppress_reason = f"Previous reply recorded as '{neg_reply['category']}'"

    if is_suppressed:
        gates["suppression"] = GateResult(name="GATE G — Suppression", passed=False, reason=suppress_reason)
        failed.append("suppression")
    else:
        gates["suppression"] = GateResult(name="GATE G — Suppression", passed=True, reason="Recipient is unsuppressed")
        passed.append("suppression")

    # 2. GATE H — Contact History & Duplicate Protection (Hard gate: cannot be bypassed)
    already_sent = conn.execute(
        "SELECT id FROM messages WHERE lead_id=? AND step=0 AND status='sent' LIMIT 1",
        (lead_id,)
    ).fetchone()
    other_sent = conn.execute(
        "SELECT m.id FROM messages m JOIN leads l ON l.id=m.lead_id WHERE l.email=? AND l.id!=? AND m.status='sent' LIMIT 1",
        (email, lead_id)
    ).fetchone()

    if already_sent:
        gates["contact_history"] = GateResult(name="GATE H — Contact History", passed=False, reason="Initial outreach already sent for this lead")
        failed.append("contact_history")
    elif other_sent:
        gates["contact_history"] = GateResult(name="GATE H — Contact History", passed=False, reason=f"Email {email} already received outreach in another record")
        failed.append("contact_history")
    elif lead_status == "active":
        gates["contact_history"] = GateResult(name="GATE H — Contact History", passed=False, reason="Sequence is already active for this lead")
        failed.append("contact_history")
    else:
        gates["contact_history"] = GateResult(name="GATE H — Contact History", passed=True, reason="No duplicate contact found")
        passed.append("contact_history")

    # 3. Retrieve / compute verification, evidence, and opportunity score
    verification = get_lead_verification_detail(lead, conn=conn)
    evidence = extract_evidence_from_lead(lead)

    # Opportunity score
    score_total_val = lead.get("score_total") if isinstance(lead, dict) else getattr(lead, "score_total", None)
    if score_total_val is not None and score_total_val > 0:
        score_band = "Priority A" if score_total_val >= 85 else "Priority B" if score_total_val >= 75 else "Manual Review" if score_total_val >= 60 else "Reject"
        opp_score = OpportunityScore(
            score_total=int(score_total_val),
            score_band=score_band,
            reason_summary=(lead.get("score_reason_summary") if isinstance(lead, dict) else getattr(lead, "score_reason_summary", "")) or "",
            timestamp=datetime.now(timezone.utc).isoformat()
        )
    else:
        opp_score = score_lead(lead, verification=verification, evidence=evidence, conn=conn)

    # GATE A — Opportunity Quality
    min_score = qc["min_opportunity_score"]
    if opp_score.score_total >= min_score:
        gates["opportunity_score"] = GateResult(
            name="GATE A — Opportunity Quality",
            passed=True,
            reason=f"Opportunity score {opp_score.score_total}/100 ({opp_score.score_band}) meets threshold >={min_score}",
            details={"score_total": opp_score.score_total, "score_band": opp_score.score_band}
        )
        passed.append("opportunity_score")
    else:
        gates["opportunity_score"] = GateResult(
            name="GATE A — Opportunity Quality",
            passed=False,
            reason=f"Opportunity score {opp_score.score_total}/100 ({opp_score.score_band}) below required threshold >={min_score}",
            details={"score_total": opp_score.score_total, "score_band": opp_score.score_band}
        )
        failed.append("opportunity_score")

    # Check for AI application rejection
    from .sources import rejects_ai_application
    if any(rejects_ai_application(lead.get(f, "") if isinstance(lead, dict) else getattr(lead, f, ""))
           for f in ("source_text", "site_text", "notes", "research")):
        gates["ai_rejection"] = GateResult(
            name="AI Application Rejection",
            passed=False,
            reason="Their post or site says AI-written applications are rejected; reply by hand instead"
        )
        failed.append("ai_rejection")

    # GATE B — Verified Contact
    from .sender import TRUSTED_SOURCES
    email_src = (lead.get("email_source") if isinstance(lead, dict) else getattr(lead, "email_source", "")) or ""
    v_passed, v_reason = evaluate_email_verification(verification, min_confidence=qc["min_email_confidence"])
    if email_src and email_src not in TRUSTED_SOURCES:
        v_passed = False
        v_reason = f"First email needs a public or provider-verified address, got '{email_src}'"

    if v_passed:
        gates["email_verification"] = GateResult(
            name="GATE B — Verified Contact",
            passed=True,
            reason=v_reason,
            details=verification.model_dump()
        )
        passed.append("email_verification")
    else:
        gates["email_verification"] = GateResult(
            name="GATE B — Verified Contact",
            passed=False,
            reason=v_reason,
            details=verification.model_dump()
        )
        failed.append("email_verification")

    # GATE C — Contact Relevance
    from .scoring import calculate_contact_relevance
    role_score, role_reason = calculate_contact_relevance(lead)
    if role_score >= 50.0:
        gates["contact_relevance"] = GateResult(
            name="GATE C — Contact Relevance",
            passed=True,
            reason=f"Relevant contact: {role_reason}",
            details={"role_score": role_score}
        )
        passed.append("contact_relevance")
    else:
        gates["contact_relevance"] = GateResult(
            name="GATE C — Contact Relevance",
            passed=False,
            reason=f"Inappropriate or irrelevant contact: {role_reason}",
            details={"role_score": role_score}
        )
        failed.append("contact_relevance")

    # GATE D — Evidence-Backed Personalization
    verified_facts = filter_verified_facts(evidence)
    n_facts = len(verified_facts)
    min_facts = qc["min_verified_personalization_facts"]
    if n_facts >= min_facts:
        gates["evidence_quality"] = GateResult(
            name="GATE D — Evidence-Backed Personalization",
            passed=True,
            reason=f"Sufficient verified evidence facts: {n_facts} (required >={min_facts})",
            details={"verified_facts_count": n_facts}
        )
        passed.append("evidence_quality")
    else:
        gates["evidence_quality"] = GateResult(
            name="GATE D — Evidence-Backed Personalization",
            passed=False,
            reason=f"Insufficient verified personalization facts: {n_facts} (required >={min_facts})",
            details={"verified_facts_count": n_facts}
        )
        failed.append("evidence_quality")

    # GATE E — Personalization Confidence
    conf = msg.get("confidence") if isinstance(msg, dict) else getattr(msg, "confidence", None)
    min_conf = qc["min_personalization_confidence"]
    approved_by = msg.get("approved_by") if isinstance(msg, dict) else getattr(msg, "approved_by", "")
    override = bool(msg.get("override") if isinstance(msg, dict) else getattr(msg, "override", 0))

    if conf is not None and float(conf) >= min_conf:
        gates["personalization_confidence"] = GateResult(
            name="GATE E — Personalization Confidence",
            passed=True,
            reason=f"Personalization confidence {float(conf):.2f} meets threshold >={min_conf}",
            details={"confidence": float(conf)}
        )
        passed.append("personalization_confidence")
    elif approved_by and approved_by != "auto":
        gates["personalization_confidence"] = GateResult(
            name="GATE E — Personalization Confidence",
            passed=True,
            reason=f"Personalization explicitly reviewed and approved by human ({approved_by})",
            details={"confidence": float(conf) if conf is not None else 0.0, "approved_by": approved_by}
        )
        passed.append("personalization_confidence")
    elif override:
        gates["personalization_confidence"] = GateResult(
            name="GATE E — Personalization Confidence",
            passed=True,
            reason="Personalization confidence overridden by user",
            details={"confidence": float(conf) if conf is not None else 0.0, "override": True}
        )
        passed.append("personalization_confidence")
    else:
        conf_str = f"{float(conf):.2f}" if conf is not None else "unknown"
        gates["personalization_confidence"] = GateResult(
            name="GATE E — Personalization Confidence",
            passed=False,
            reason=f"Personalization confidence {conf_str} below required threshold >={min_conf}",
            details={"confidence": conf}
        )
        failed.append("personalization_confidence")

    # GATE F — Human Approval
    msg_status = msg.get("status") or ""
    if msg_status == "approved" and (override or (approved_by and approved_by != "auto")):
        gates["approval"] = GateResult(
            name="GATE F — Human Approval",
            passed=True,
            reason="Initial outreach has explicit human approval" if not override else "Explicitly approved via Send Anyway override",
            details={"status": msg_status, "approved_by": approved_by, "override": override}
        )
        passed.append("approval")
    elif msg_status == "approved" and approved_by == "auto":
        gates["approval"] = GateResult(
            name="GATE F — Human Approval",
            passed=False,
            reason="Initial outreach cannot automatically bypass human review (auto-approval forbidden)",
            details={"status": msg_status, "approved_by": "auto"}
        )
        failed.append("approval")
    elif msg_status == "approved":
        # Approved without explicit by
        gates["approval"] = GateResult(
            name="GATE F — Human Approval",
            passed=True,
            reason="Draft has been approved",
            details={"status": msg_status}
        )
        passed.append("approval")
    else:
        gates["approval"] = GateResult(
            name="GATE F — Human Approval",
            passed=False,
            reason=f"Initial outreach requires explicit human approval (current status: '{msg_status}')",
            details={"status": msg_status}
        )
        failed.append("approval")

    # GATE I — Daily Quota
    s = config.settings().get("sending", {})
    tz = ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))
    today = now.astimezone(tz).date().isoformat()
    boxes = [b for b in config.inboxes() if b.get("enabled", True)]
    first_sent_today = sum(db.send_count(conn, today, b["email"], "first") for b in boxes)

    counts = db.get_daily_allocation_counts(conn, today)
    alloc = config.allocation_settings()
    ceiling = alloc.get("daily_new_limit", alloc.get("daily_send_limit", 28))
    # Cap applies strictly to new initial outreach emails (step == 0)
    effective_sent = max(first_sent_today, counts.get("freelance", 0) + counts.get("internship", 0))

    if effective_sent >= ceiling:
        gates["quota"] = GateResult(
            name="GATE I — Daily Quota",
            passed=False,
            reason=f"Daily send ceiling reached: {effective_sent}/{ceiling} sent today",
            details={"sent_today": effective_sent, "ceiling": ceiling}
        )
        failed.append("quota")
    else:
        from .scoring import get_opportunity_mode, MODE_FREELANCE, MODE_INTERNSHIP
        mode = get_opportunity_mode(lead)
        counts = db.get_daily_allocation_counts(conn, today)
        if mode == MODE_FREELANCE:
            cat_sent = counts.get("freelance", 0)
            cat_limit = alloc.get("daily_freelance_new_limit", 12)
        else:
            cat_sent = counts.get("internship", 0)
            cat_limit = alloc.get("daily_internship_new_limit", 8)

        realloc = alloc.get("allow_unused_quota_reallocation", False)
        if cat_sent >= cat_limit and not realloc:
            gates["quota"] = GateResult(
                name="GATE I — Daily Quota",
                passed=False,
                reason=f"Daily {mode} quota reached: {cat_sent}/{cat_limit} sent today (reallocation disabled)",
                details={"sent_today": effective_sent, "ceiling": ceiling, "mode": mode, "cat_sent": cat_sent, "cat_limit": cat_limit}
            )
            failed.append("quota")
        else:
            gates["quota"] = GateResult(
                name="GATE I — Daily Quota",
                passed=True,
                reason=f"Daily ceiling available: {effective_sent}/{ceiling} sent today (mode {mode}: {cat_sent}/{cat_limit})",
                details={"sent_today": effective_sent, "ceiling": ceiling, "mode": mode, "cat_sent": cat_sent, "cat_limit": cat_limit}
            )
            passed.append("quota")

    # GATE J — Sending Window
    from . import sender
    segment_name = lead.get("segment") or ""
    seg = config.segment(segment_name) if segment_name in config.settings().get("segments", {}) else {}
    if not seg or sender.in_window(seg, now):
        gates["timing"] = GateResult(
            name="GATE J — Sending Window",
            passed=True,
            reason="Current time is inside configured sending window",
            details={"segment": segment_name}
        )
        passed.append("timing")
    else:
        gates["timing"] = GateResult(
            name="GATE J — Sending Window",
            passed=False,
            reason=f"Current time is outside sending window for segment '{segment_name}'",
            details={"segment": segment_name}
        )
        failed.append("timing")

    eligible = len(failed) == 0
    summary = "Eligible to send" if eligible else f"Blocked by gates: {', '.join(failed)}"

    return EligibilityResult(
        eligible=eligible,
        summary=summary,
        failed_gates=failed,
        passed_gates=passed,
        reasons=gates,
        score_total=opp_score.score_total,
        score_band=opp_score.score_band,
        is_followup=False,
    )


def _evaluate_followup_eligibility(
    lead: dict | Any,
    msg: dict | Any,
    conn: Any,
    now: datetime,
    qc: dict[str, Any]
) -> EligibilityResult:
    """Evaluate separate eligibility policy for sequence follow-ups."""
    lead = dict(lead) if lead is not None else {}
    msg = dict(msg) if msg is not None else {}
    gates: dict[str, GateResult] = {}
    failed: list[str] = []
    passed: list[str] = []

    lead_id = lead.get("id")
    email = lead.get("email") or ""
    step = int(msg.get("step") or 1)

    # 0. Content Integrity (Check message text for placeholders, mock patterns)
    valid_content, content_reason = validate_message_content(lead, msg)
    if not valid_content:
        gates["content_integrity"] = GateResult(name="Content Integrity", passed=False, reason=content_reason)
        failed.append("content_integrity")
    else:
        gates["content_integrity"] = GateResult(name="Content Integrity", passed=True, reason="Follow-up text passed integrity check")
        passed.append("content_integrity")

    # 1. Previous sequence step was actually sent
    prev = conn.execute(
        "SELECT id, status FROM messages WHERE lead_id=? AND step=? LIMIT 1",
        (lead_id, step - 1)
    ).fetchone()
    if prev and prev["status"] == "sent":
        gates["previous_step"] = GateResult(name="Previous Step Sent", passed=True, reason=f"Step {step-1} was sent successfully")
        passed.append("previous_step")
    else:
        prev_st = prev["status"] if prev else "missing"
        gates["previous_step"] = GateResult(name="Previous Step Sent", passed=False, reason=f"Previous step {step-1} has status '{prev_st}', requires 'sent'")
        failed.append("previous_step")

    # 2. No reply has been received (stop pending follow-ups immediately on reply)
    lead_status = (lead.get("status") if isinstance(lead, dict) else getattr(lead, "status", "")) or ""
    any_reply = conn.execute(
        "SELECT id, category FROM replies WHERE lead_id=? LIMIT 1",
        (lead_id,)
    ).fetchone()

    if lead_status == "replied" or any_reply:
        cat = any_reply["category"] if any_reply else "reply"
        gates["no_reply"] = GateResult(name="No Reply Received", passed=False, reason=f"Reply received from recipient (category: {cat}); follow-ups halted")
        failed.append("no_reply")
    else:
        gates["no_reply"] = GateResult(name="No Reply Received", passed=True, reason="No reply received yet")
        passed.append("no_reply")

    # 3. Unsuppressed & Unbounced
    if db.suppressed(conn, email) or lead_status in ("unsubscribed", "bounced", "rejected"):
        gates["suppression"] = GateResult(name="Unsuppressed", passed=False, reason=f"Recipient is suppressed/bounced (status: '{lead_status}')")
        failed.append("suppression")
    else:
        gates["suppression"] = GateResult(name="Unsuppressed", passed=True, reason="Recipient is not suppressed")
        passed.append("suppression")

    # 4. Minimum delay has elapsed (due_at <= now)
    due_at = (msg.get("due_at") if isinstance(msg, dict) else getattr(msg, "due_at", None)) or ""
    now_iso = now.isoformat()
    if due_at and due_at <= now_iso:
        gates["timing_delay"] = GateResult(name="Due Date Elapsed", passed=True, reason=f"Follow-up is due ({due_at} <= {now_iso})")
        passed.append("timing_delay")
    else:
        gates["timing_delay"] = GateResult(name="Due Date Elapsed", passed=False, reason=f"Follow-up is not due yet (due: {due_at})")
        failed.append("timing_delay")

    # 5. Sequence is active
    if lead_status == "active":
        gates["sequence_active"] = GateResult(name="Sequence Active", passed=True, reason="Lead sequence is currently active")
        passed.append("sequence_active")
    else:
        gates["sequence_active"] = GateResult(name="Sequence Active", passed=False, reason=f"Lead status is '{lead_status}', requires 'active'")
        failed.append("sequence_active")

    # 6. Sending window
    from . import sender
    segment_name = lead.get("segment") or ""
    seg = config.segment(segment_name) if segment_name in config.settings().get("segments", {}) else {}
    if not seg or sender.in_window(seg, now):
        gates["sending_window"] = GateResult(name="Sending Window", passed=True, reason="Within sending window")
        passed.append("sending_window")
    else:
        gates["sending_window"] = GateResult(name="Sending Window", passed=False, reason="Outside sending window")
        failed.append("sending_window")

    # 7. Daily quota has space
    s = config.settings().get("sending", {})
    tz = ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))
    today = now.astimezone(tz).date().isoformat()
    boxes = [b for b in config.inboxes() if b.get("enabled", True)]
    total_sent_today = sum(db.send_count(conn, today, b["email"]) for b in boxes)
    first_sent_today = sum(db.send_count(conn, today, b["email"], "first") for b in boxes)
    followup_sent_today = sum(db.send_count(conn, today, b["email"], "followup") for b in boxes)
    if total_sent_today == 0 and (first_sent_today > 0 or followup_sent_today > 0):
        total_sent_today = first_sent_today + followup_sent_today

    # 7. Daily quota check for follow-up
    # The 28 daily limit applies strictly to new initial outreach emails (step == 0).
    # Follow-ups are unconstrained and do not count against or get blocked by the 28 new email cap.
    alloc = config.allocation_settings()
    followup_limit = alloc.get("daily_followup_limit", 0)
    realloc = alloc.get("allow_unused_quota_reallocation", True)
    counts = db.get_daily_allocation_counts(conn, today)
    fu_sent = max(followup_sent_today, counts.get("followup", 0))

    if followup_limit > 0 and fu_sent >= followup_limit and not realloc:
        gates["quota"] = GateResult(name="Daily Quota", passed=False, reason=f"Daily follow-up quota reached ({fu_sent}/{followup_limit})")
        failed.append("quota")
    else:
        gates["quota"] = GateResult(name="Daily Quota", passed=True, reason=f"Follow-ups unconstrained (sent today: {fu_sent})")
        passed.append("quota")

    eligible = len(failed) == 0
    summary = "Follow-up eligible" if eligible else f"Follow-up blocked: {', '.join(failed)}"

    return EligibilityResult(
        eligible=eligible,
        summary=summary,
        failed_gates=failed,
        passed_gates=passed,
        reasons=gates,
        score_total=int((lead.get("score_total") if isinstance(lead, dict) else getattr(lead, "score_total", 0)) or 0),
        score_band=(lead.get("score_band") if isinstance(lead, dict) else getattr(lead, "score_band", "")) or "",
        is_followup=True,
    )
