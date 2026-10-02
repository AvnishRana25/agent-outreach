"""Phase 3 outcome analytics, multi-dimensional reporting, and safe learning engine.

Calculates grounded conversion metrics:
- delivery rate, bounce rate, reply rate, positive reply rate, meeting rate,
  interview rate, project-discussion rate, offer rate, project-win rate.
- NO OPEN RATES (inaccurate and vanity).

Answers key strategic questions:
- Slot utilization & gate rejections breakdown
- Source performance (meetings/interviews/wins)
- Priority A vs Priority B conversion
- Contact role performance
- Bounce rate by email confidence tier
- CTA style and message angle performance
- Mode comparison (FREELANCE vs INTERNSHIP)
- Safe advisory recommendations (never modifies prompts/weights without human approval).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any

from . import config, db


def calculate_outcome_metrics(conn, mode: str | None = None, days: int | None = None) -> dict[str, Any]:
    """Calculate grounded conversion and outcome metrics across messages."""
    where_clauses = ["1=1"]
    args: list[Any] = []

    if mode:
        where_clauses.append("mode = ?")
        args.append(mode)

    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)

    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"SELECT outcome, COUNT(*) as cnt FROM message_outcomes WHERE {where_sql} GROUP BY outcome",
        tuple(args)
    ).fetchall()
    counts = {r["outcome"]: int(r["cnt"]) for r in rows}

    # If message_outcomes has data, use it; otherwise compute from leads/messages/replies
    total_outcomes = sum(counts.values())
    if total_outcomes > 0:
        bounced = counts.get("bounced", 0)
        delivered = total_outcomes - bounced
        replied = sum(counts.get(k, 0) for k in (
            "replied", "positive_reply", "negative_reply", "meeting",
            "interview", "project_discussion", "offer", "won_project", "lost_project"
        ))
        positive = sum(counts.get(k, 0) for k in (
            "positive_reply", "meeting", "interview", "project_discussion", "offer", "won_project"
        ))
        meetings = counts.get("meeting", 0)
        interviews = counts.get("interview", 0)
        discussions = counts.get("project_discussion", 0)
        offers = counts.get("offer", 0)
        wins = counts.get("won_project", 0)
        lost = counts.get("lost_project", 0)
    else:
        # Fallback to computing from existing leads, messages, and replies
        base_where = ["m.status = 'sent'"]
        base_args: list[Any] = []
        if mode == "freelance":
            base_where.append("(l.segment NOT LIKE '%intern%' AND COALESCE(l.opportunity_type, '') NOT IN ('internship', 'intern'))")
        elif mode == "internship":
            base_where.append("(l.segment LIKE '%intern%' OR l.opportunity_type IN ('internship', 'intern'))")
        if days:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            base_where.append("m.sent_at >= ?")
            base_args.append(since)

        b_sql = " AND ".join(base_where)
        total_outcomes = conn.execute(
            f"SELECT COUNT(*) FROM messages m JOIN leads l ON l.id = m.lead_id WHERE {b_sql}",
            tuple(base_args)
        ).fetchone()[0]

        bounced = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND (l.status = 'bounced' OR l.email_status = 'invalid')",
            tuple(base_args)
        ).fetchone()[0]

        delivered = max(0, total_outcomes - bounced)

        replied = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"JOIN replies r ON r.lead_id = l.id "
            f"WHERE {b_sql} AND r.category NOT IN ('bounce', 'out_of_office')",
            tuple(base_args)
        ).fetchone()[0]

        pos_sql = "('interested','meeting_request','question','referral')"
        positive = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"JOIN replies r ON r.lead_id = l.id "
            f"WHERE {b_sql} AND r.category IN {pos_sql}",
            tuple(base_args)
        ).fetchone()[0]

        meetings = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND l.deal_stage IN ('call_booked', 'proposal_sent', 'won') "
            f"AND (l.segment NOT LIKE '%intern%' AND COALESCE(l.opportunity_type, '') NOT IN ('internship', 'intern'))",
            tuple(base_args)
        ).fetchone()[0]

        interviews = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND l.deal_stage IN ('call_booked', 'proposal_sent', 'won') "
            f"AND (l.segment LIKE '%intern%' OR l.opportunity_type IN ('internship', 'intern'))",
            tuple(base_args)
        ).fetchone()[0]

        discussions = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND l.deal_stage IN ('proposal_sent', 'won')",
            tuple(base_args)
        ).fetchone()[0]

        offers = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND l.deal_stage = 'won' "
            f"AND (l.segment LIKE '%intern%' OR l.opportunity_type IN ('internship', 'intern'))",
            tuple(base_args)
        ).fetchone()[0]

        wins = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND l.deal_stage = 'won' "
            f"AND (l.segment NOT LIKE '%intern%' AND COALESCE(l.opportunity_type, '') NOT IN ('internship', 'intern'))",
            tuple(base_args)
        ).fetchone()[0]

        lost = conn.execute(
            f"SELECT COUNT(DISTINCT l.id) FROM messages m JOIN leads l ON l.id = m.lead_id "
            f"WHERE {b_sql} AND l.deal_stage = 'lost'",
            tuple(base_args)
        ).fetchone()[0]

    delivery_rate = round(100.0 * delivered / total_outcomes, 1) if total_outcomes > 0 else 0.0
    bounce_rate = round(100.0 * bounced / total_outcomes, 1) if total_outcomes > 0 else 0.0
    reply_rate = round(100.0 * replied / delivered, 1) if delivered > 0 else 0.0
    positive_reply_rate = round(100.0 * positive / delivered, 1) if delivered > 0 else 0.0
    meeting_rate = round(100.0 * meetings / delivered, 1) if delivered > 0 else 0.0
    interview_rate = round(100.0 * interviews / delivered, 1) if delivered > 0 else 0.0
    discussion_rate = round(100.0 * discussions / delivered, 1) if delivered > 0 else 0.0
    offer_rate = round(100.0 * offers / delivered, 1) if delivered > 0 else 0.0
    win_rate = round(100.0 * wins / delivered, 1) if delivered > 0 else 0.0

    return {
        "mode": mode or "all",
        "total_sent": total_outcomes,
        "delivered": delivered,
        "bounced": bounced,
        "replied": replied,
        "positive_reply": positive,
        "meeting": meetings,
        "interview": interviews,
        "project_discussion": discussions,
        "offer": offers,
        "won_project": wins,
        "lost_project": lost,
        "delivery_rate": delivery_rate,
        "bounce_rate": bounce_rate,
        "reply_rate": reply_rate,
        "positive_reply_rate": positive_reply_rate,
        "meeting_rate": meeting_rate,
        "interview_rate": interview_rate,
        "project_discussion_rate": discussion_rate,
        "offer_rate": offer_rate,
        "project_win_rate": win_rate,
    }


def get_mode_comparison(conn, days: int | None = None) -> dict[str, dict[str, Any]]:
    """Compare freelance vs internship performance across all funnel stages."""
    return {
        "freelance": calculate_outcome_metrics(conn, mode="freelance", days=days),
        "internship": calculate_outcome_metrics(conn, mode="internship", days=days),
        "total": calculate_outcome_metrics(conn, mode=None, days=days),
    }


def get_priority_comparison(conn, days: int | None = None) -> list[dict[str, Any]]:
    """Compare conversion metrics between Priority A (>=85) and Priority B (75-84)."""
    where_clauses = ["1=1"]
    args: list[Any] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)
    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT
            CASE
                WHEN opportunity_score >= 85 THEN 'Priority A (85-100)'
                WHEN opportunity_score >= 75 THEN 'Priority B (75-84)'
                ELSE 'Other (<75)'
            END AS band,
            COUNT(*) as sent,
            SUM(CASE WHEN outcome = 'bounced' THEN 1 ELSE 0 END) as bounced,
            SUM(CASE WHEN outcome IN ('replied','positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as replied,
            SUM(CASE WHEN outcome IN ('positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as positive
            FROM message_outcomes
            WHERE {where_sql}
            GROUP BY band ORDER BY band""",
        tuple(args)
    ).fetchall()

    results = []
    for r in rows:
        sent = r["sent"]
        bounced = r["bounced"]
        delivered = max(0, sent - bounced)
        replied = r["replied"]
        pos = r["positive"]
        results.append({
            "band": r["band"],
            "sent": sent,
            "delivered": delivered,
            "bounced": bounced,
            "replied": replied,
            "positive": pos,
            "bounce_rate": round(100.0 * bounced / sent, 1) if sent > 0 else 0.0,
            "reply_rate": round(100.0 * replied / delivered, 1) if delivered > 0 else 0.0,
            "positive_rate": round(100.0 * pos / delivered, 1) if delivered > 0 else 0.0,
        })
    return results


def get_source_performance(conn, days: int | None = None) -> list[dict[str, Any]]:
    """Compare performance across discovery sources."""
    where_clauses = ["1=1"]
    args: list[Any] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)
    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT
            COALESCE(NULLIF(lead_source, ''), 'unknown') as source,
            mode,
            COUNT(*) as sent,
            SUM(CASE WHEN outcome = 'bounced' THEN 1 ELSE 0 END) as bounced,
            SUM(CASE WHEN outcome IN ('replied','positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as replied,
            SUM(CASE WHEN outcome IN ('positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as positive,
            SUM(CASE WHEN outcome IN ('meeting','interview') THEN 1 ELSE 0 END) as meetings_or_interviews,
            SUM(CASE WHEN outcome IN ('won_project','offer') THEN 1 ELSE 0 END) as wins_or_offers
            FROM message_outcomes
            WHERE {where_sql}
            GROUP BY source, mode ORDER BY sent DESC""",
        tuple(args)
    ).fetchall()

    results = []
    for r in rows:
        sent = r["sent"]
        bounced = r["bounced"]
        delivered = max(0, sent - bounced)
        results.append({
            "source": r["source"],
            "mode": r["mode"],
            "sent": sent,
            "delivered": delivered,
            "bounced": bounced,
            "replied": r["replied"],
            "positive": r["positive"],
            "meetings_or_interviews": r["meetings_or_interviews"],
            "wins_or_offers": r["wins_or_offers"],
            "reply_rate": round(100.0 * r["replied"] / delivered, 1) if delivered > 0 else 0.0,
            "positive_rate": round(100.0 * r["positive"] / delivered, 1) if delivered > 0 else 0.0,
        })
    return results


def get_role_performance(conn, days: int | None = None) -> list[dict[str, Any]]:
    """Performance by contact role (e.g. founder, CTO, engineering manager, recruiter)."""
    where_clauses = ["1=1"]
    args: list[Any] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)
    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT
            CASE
                WHEN lower(contact_role) LIKE '%founder%' OR lower(contact_role) LIKE '%ceo%' OR lower(contact_role) LIKE '%owner%' THEN 'Founder / CEO'
                WHEN lower(contact_role) LIKE '%cto%' OR lower(contact_role) LIKE '%chief tech%' THEN 'CTO / Tech Lead'
                WHEN lower(contact_role) LIKE '%manager%' OR lower(contact_role) LIKE '%lead%' THEN 'Engineering Manager'
                WHEN lower(contact_role) LIKE '%recruiter%' OR lower(contact_role) LIKE '%talent%' THEN 'Recruiter / Talent'
                WHEN lower(contact_role) LIKE '%operations%' OR lower(contact_role) LIKE '%ops%' THEN 'Operations Head'
                WHEN contact_role = '' OR contact_role IS NULL THEN 'Unspecified'
                ELSE 'Other Role'
            END as role_group,
            mode,
            COUNT(*) as sent,
            SUM(CASE WHEN outcome = 'bounced' THEN 1 ELSE 0 END) as bounced,
            SUM(CASE WHEN outcome IN ('replied','positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as replied,
            SUM(CASE WHEN outcome IN ('positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as positive
            FROM message_outcomes
            WHERE {where_sql}
            GROUP BY role_group, mode ORDER BY sent DESC""",
        tuple(args)
    ).fetchall()

    results = []
    for r in rows:
        sent = r["sent"]
        bounced = r["bounced"]
        delivered = max(0, sent - bounced)
        results.append({
            "role_group": r["role_group"],
            "mode": r["mode"],
            "sent": sent,
            "delivered": delivered,
            "bounced": bounced,
            "replied": r["replied"],
            "positive": r["positive"],
            "reply_rate": round(100.0 * r["replied"] / delivered, 1) if delivered > 0 else 0.0,
            "positive_rate": round(100.0 * r["positive"] / delivered, 1) if delivered > 0 else 0.0,
        })
    return results


def get_email_confidence_breakdown(conn, days: int | None = None) -> list[dict[str, Any]]:
    """Bounce and reply rates grouped by email confidence tier."""
    where_clauses = ["1=1"]
    args: list[Any] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)
    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT
            CASE
                WHEN email_confidence >= 90 THEN 'Tier 1: High (90-100%)'
                WHEN email_confidence >= 80 THEN 'Tier 2: Medium (80-89%)'
                WHEN email_confidence > 0 THEN 'Tier 3: Risky (<80%)'
                ELSE 'Tier 4: Unverified'
            END as tier,
            COUNT(*) as sent,
            SUM(CASE WHEN outcome = 'bounced' THEN 1 ELSE 0 END) as bounced,
            SUM(CASE WHEN outcome IN ('replied','positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as replied
            FROM message_outcomes
            WHERE {where_sql}
            GROUP BY tier ORDER BY tier""",
        tuple(args)
    ).fetchall()

    results = []
    for r in rows:
        sent = r["sent"]
        bounced = r["bounced"]
        delivered = max(0, sent - bounced)
        results.append({
            "tier": r["tier"],
            "sent": sent,
            "delivered": delivered,
            "bounced": bounced,
            "replied": r["replied"],
            "bounce_rate": round(100.0 * bounced / sent, 1) if sent > 0 else 0.0,
            "reply_rate": round(100.0 * r["replied"] / delivered, 1) if delivered > 0 else 0.0,
        })
    return results


def get_cta_performance(conn, days: int | None = None) -> list[dict[str, Any]]:
    """Performance by Call-to-Action style."""
    where_clauses = ["cta_type != ''"]
    args: list[Any] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)
    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT
            cta_type,
            mode,
            COUNT(*) as sent,
            SUM(CASE WHEN outcome = 'bounced' THEN 1 ELSE 0 END) as bounced,
            SUM(CASE WHEN outcome IN ('replied','positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as replied,
            SUM(CASE WHEN outcome IN ('positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as positive
            FROM message_outcomes
            WHERE {where_sql}
            GROUP BY cta_type, mode ORDER BY sent DESC""",
        tuple(args)
    ).fetchall()

    results = []
    for r in rows:
        sent = r["sent"]
        bounced = r["bounced"]
        delivered = max(0, sent - bounced)
        results.append({
            "cta_type": r["cta_type"],
            "mode": r["mode"],
            "sent": sent,
            "delivered": delivered,
            "replied": r["replied"],
            "positive": r["positive"],
            "positive_rate": round(100.0 * r["positive"] / delivered, 1) if delivered > 0 else 0.0,
        })
    return results


def get_angle_performance(conn, days: int | None = None) -> list[dict[str, Any]]:
    """Performance by message angle."""
    where_clauses = ["message_angle != ''"]
    args: list[Any] = []
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        where_clauses.append("sent_at >= ?")
        args.append(since)
    where_sql = " AND ".join(where_clauses)

    rows = conn.execute(
        f"""SELECT
            message_angle,
            mode,
            COUNT(*) as sent,
            SUM(CASE WHEN outcome = 'bounced' THEN 1 ELSE 0 END) as bounced,
            SUM(CASE WHEN outcome IN ('replied','positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as replied,
            SUM(CASE WHEN outcome IN ('positive_reply','meeting','interview','project_discussion','offer','won_project') THEN 1 ELSE 0 END) as positive
            FROM message_outcomes
            WHERE {where_sql}
            GROUP BY message_angle, mode ORDER BY sent DESC""",
        tuple(args)
    ).fetchall()

    results = []
    for r in rows:
        sent = r["sent"]
        bounced = r["bounced"]
        delivered = max(0, sent - bounced)
        results.append({
            "message_angle": r["message_angle"],
            "mode": r["mode"],
            "sent": sent,
            "delivered": delivered,
            "replied": r["replied"],
            "positive": r["positive"],
            "positive_rate": round(100.0 * r["positive"] / delivered, 1) if delivered > 0 else 0.0,
        })
    return results


def get_slot_utilization(conn, target_date: str | None = None) -> dict[str, Any]:
    """Examine daily send capacity vs 28 ceiling and breakdown of send-blocking reasons."""
    day = target_date or datetime.now(timezone.utc).date().isoformat()
    alloc = config.allocation_settings()
    counts = db.get_daily_allocation_counts(conn, day)

    total_sent = counts.get("total", 0)
    ceiling = alloc.get("daily_send_limit", 28)
    fl_sent = counts.get("freelance", 0)
    fl_limit = alloc.get("daily_freelance_new_limit", 12)
    it_sent = counts.get("internship", 0)
    it_limit = alloc.get("daily_internship_new_limit", 8)
    fu_sent = counts.get("followup", 0)
    fu_limit = alloc.get("daily_followup_limit", 8)

    # Determine what stopped sends if fewer than 28 were sent
    gap = max(0, ceiling - total_sent)
    blocking_reasons: dict[str, int] = {}

    if gap > 0:
        # Check held messages and draft counts
        held_rows = conn.execute("SELECT hold, COUNT(*) as cnt FROM messages WHERE hold != '' GROUP BY hold").fetchall()
        for r in held_rows:
            reason = r["hold"]
            if "score" in reason:
                blocking_reasons["low_opportunity_score"] = blocking_reasons.get("low_opportunity_score", 0) + int(r["cnt"])
            elif "address" in reason or "valid" in reason:
                blocking_reasons["unverified_email"] = blocking_reasons.get("unverified_email", 0) + int(r["cnt"])
            elif "evidence" in reason:
                blocking_reasons["weak_evidence"] = blocking_reasons.get("weak_evidence", 0) + int(r["cnt"])
            elif "approval" in reason or "review" in reason:
                blocking_reasons["awaiting_approval"] = blocking_reasons.get("awaiting_approval", 0) + int(r["cnt"])
            else:
                blocking_reasons["other_quality_hold"] = blocking_reasons.get("other_quality_hold", 0) + int(r["cnt"])

        # Check drafts awaiting review
        pending_review = conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'draft' AND step = 0").fetchone()[0]
        if pending_review > 0:
            blocking_reasons["awaiting_human_review"] = pending_review

        # Check total researched leads available
        available_leads = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'researched'").fetchone()[0]
        if available_leads < gap:
            blocking_reasons["insufficient_leads_in_pipeline"] = gap - available_leads

    return {
        "date": day,
        "ceiling": ceiling,
        "total_sent": total_sent,
        "utilization_pct": round(100.0 * total_sent / ceiling, 1) if ceiling > 0 else 0.0,
        "allocations": {
            "freelance": {"sent": fl_sent, "limit": fl_limit, "pct": round(100.0 * fl_sent / fl_limit, 1) if fl_limit > 0 else 0.0},
            "internship": {"sent": it_sent, "limit": it_limit, "pct": round(100.0 * it_sent / it_limit, 1) if it_limit > 0 else 0.0},
            "followup": {"sent": fu_sent, "limit": fu_limit, "pct": round(100.0 * fu_sent / fu_limit, 1) if fu_limit > 0 else 0.0},
        },
        "reallocation_enabled": alloc.get("allow_unused_quota_reallocation", False),
        "blocking_reasons": blocking_reasons,
    }


def get_recommendations(conn) -> list[dict[str, str]]:
    """Generate safe, advisory recommendations based on outcome empirical data.
    
    SAFETY RULE: Proposes adjustments only. Never automatically rewrites weights or prompts.
    """
    recs: list[dict[str, str]] = []

    # 1. Email confidence & bounce analysis
    conf_data = get_email_confidence_breakdown(conn)
    for t in conf_data:
        if "Risky" in t["tier"] and t["bounce_rate"] > 5.0 and t["sent"] >= 10:
            recs.append({
                "area": "Contact Verification",
                "finding": f"{t['tier']} has high bounce rate of {t['bounce_rate']}%.",
                "recommendation": "Raise MIN_EMAIL_CONFIDENCE threshold in settings.yaml from 80 to 85 to protect domain reputation.",
                "action_required": "Manual human review and configuration update required in config/settings.yaml."
            })

    # 2. Priority A vs B analysis
    prio_data = get_priority_comparison(conn)
    prio_map = {p["band"]: p for p in prio_data}
    if "Priority A (85-100)" in prio_map and "Priority B (75-84)" in prio_map:
        a = prio_map["Priority A (85-100)"]
        b = prio_map["Priority B (75-84)"]
        if a["sent"] >= 10 and b["sent"] >= 10 and a["positive_rate"] >= b["positive_rate"] * 1.5:
            recs.append({
                "area": "Opportunity Scoring",
                "finding": f"Priority A positive reply rate ({a['positive_rate']}%) strongly exceeds Priority B ({b['positive_rate']}%).",
                "recommendation": "Consider tightening MIN_OPPORTUNITY_SCORE from 75 to 80 to concentrate volume on top-tier prospects.",
                "action_required": "Manual human review and configuration update required."
            })

    # 3. Role conversion analysis
    role_data = get_role_performance(conn)
    top_role = max(role_data, key=lambda x: (x["positive_rate"], x["sent"])) if role_data else None
    if top_role and top_role["sent"] >= 5 and top_role["positive_rate"] > 15.0:
        recs.append({
            "area": "Targeting",
            "finding": f"Top converting role: '{top_role['role_group']}' ({top_role['mode']}) with {top_role['positive_rate']}% positive rate.",
            "recommendation": f"Increase scoring weight for {top_role['role_group']} in {top_role['mode']} mode.",
            "action_required": "Manual human review of scoring weights."
        })

    # Default general recommendation if young pipeline
    if not recs:
        recs.append({
            "area": "Pipeline Health",
            "finding": "Baseline data collection active. No significant anomaly or bias detected.",
            "recommendation": "Continue monitoring grounded outcomes across freelance and internship funnels.",
            "action_required": "None at this stage."
        })

    return recs


def format_analytics_report(conn, mode: str | None = None, days: int = 30) -> str:
    """Format a comprehensive terminal/dashboard text report of Phase 3 outcomes."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append(f"  AGENT OUTREACH — OUTCOME ANALYTICS & CONVERSION REPORT (Past {days}d)")
    lines.append("=" * 70)

    # 1. Mode Breakdown & Comparative Funnel
    modes = [mode] if mode else ["freelance", "internship"]
    for m in modes:
        metrics = calculate_outcome_metrics(conn, mode=m, days=days)
        lines.append(f"\n[MODE: {m.upper()}]")
        lines.append(f"  Sent: {metrics['total_sent']} | Delivered: {metrics['delivered']} ({metrics['delivery_rate']}%) | Bounced: {metrics['bounced']} ({metrics['bounce_rate']}%)")
        lines.append(f"  Replies: {metrics['replied']} ({metrics['reply_rate']}%) | Positive: {metrics['positive_reply']} ({metrics['positive_reply_rate']}%)")
        if m == "freelance":
            lines.append(f"  Meetings: {metrics['meeting']} ({metrics['meeting_rate']}%) | Project Discussions: {metrics['project_discussion']} ({metrics['project_discussion_rate']}%)")
            lines.append(f"  Won Projects: {metrics['won_project']} ({metrics['project_win_rate']}%) | Lost: {metrics['lost_project']}")
        else:
            lines.append(f"  Interviews: {metrics['interview']} ({metrics['interview_rate']}%) | Discussions: {metrics['project_discussion']} ({metrics['project_discussion_rate']}%)")
            lines.append(f"  Offers: {metrics['offer']} ({metrics['offer_rate']}%)")

    # 2. Daily 28/day Slot Utilization
    util = get_slot_utilization(conn)
    lines.append("\n" + "-" * 70)
    lines.append(f"DAILY 28-EMAIL ALLOCATION & UTILIZATION (Today: {util['date']})")
    lines.append("-" * 70)
    lines.append(f"  Total Sent Today: {util['total_sent']}/{util['ceiling']} ({util['utilization_pct']}%)")
    allocs = util["allocations"]
    lines.append(f"  Freelance New:    {allocs['freelance']['sent']}/{allocs['freelance']['limit']} ({allocs['freelance']['pct']}%)")
    lines.append(f"  Internship New:   {allocs['internship']['sent']}/{allocs['internship']['limit']} ({allocs['internship']['pct']}%)")
    lines.append(f"  Follow-ups:       {allocs['followup']['sent']}/{allocs['followup']['limit']} ({allocs['followup']['pct']}%)")
    lines.append(f"  Quota Reallocation: {'ENABLED' if util['reallocation_enabled'] else 'DISABLED (unused slots remain unused)'}")
    if util["blocking_reasons"]:
        lines.append("  Send-Limiting Factors:")
        for r, cnt in util["blocking_reasons"].items():
            lines.append(f"    - {r.replace('_', ' ')}: {cnt}")

    # 3. Priority A vs B
    prios = get_priority_comparison(conn, days=days)
    if prios:
        lines.append("\n" + "-" * 70)
        lines.append("PRIORITY A VS B PERFORMANCE")
        lines.append("-" * 70)
        for p in prios:
            lines.append(f"  {p['band']:<22} Sent: {p['sent']:<4} Deliv: {p['delivered']:<4} Bnc%: {p['bounce_rate']:>5.1f}% Rep%: {p['reply_rate']:>5.1f}% Pos%: {p['positive_rate']:>5.1f}%")

    # 4. Email Confidence Bounce Analysis
    confs = get_email_confidence_breakdown(conn, days=days)
    if confs:
        lines.append("\n" + "-" * 70)
        lines.append("EMAIL CONFIDENCE & BOUNCE ANALYSIS")
        lines.append("-" * 70)
        for c in confs:
            lines.append(f"  {c['tier']:<25} Sent: {c['sent']:<4} Bounced: {c['bounced']:<3} Bounce Rate: {c['bounce_rate']:>5.1f}%")

    # 5. Role & Source Performance
    roles = get_role_performance(conn, days=days)[:6]
    if roles:
        lines.append("\n" + "-" * 70)
        lines.append("TOP CONTACT ROLES")
        lines.append("-" * 70)
        for ro in roles:
            lines.append(f"  {ro['role_group']:<22} ({ro['mode']:<10}) Sent: {ro['sent']:<3} Pos Reply: {ro['positive_rate']:>5.1f}%")

    # 6. Learning & Optimization Safety Recommendations
    recs = get_recommendations(conn)
    lines.append("\n" + "-" * 70)
    lines.append("LEARNING & OPTIMIZATION RECOMMENDATIONS (HUMAN APPROVAL REQUIRED)")
    lines.append("-" * 70)
    for rc in recs:
        lines.append(f"  [{rc['area']}]")
        lines.append(f"    Observation:    {rc['finding']}")
        lines.append(f"    Recommendation: {rc['recommendation']}")
        lines.append(f"    Action:         {rc['action_required']}")

    lines.append("=" * 70 + "\n")
    return "\n".join(lines)
