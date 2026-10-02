"""Centralized alert manager with Telegram integration and anti-spam cooldown.

Dispatches actionable alerts for critical production events:
- Remote DB unavailable
- Sending provider authentication failure
- Bounce threshold exceeded
- Duplicate-send protection triggered
- Quota integrity failure
- Stuck sending state (unreconciled messages)
- Repeated provider outage
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any
import requests

from . import config, db

_MEMORY_COOLDOWN: dict[str, float] = {}


def _can_alert(key: str, cooldown_minutes: int) -> bool:
    """Check whether cooldown has elapsed for the given alert key."""
    now_ts = time.time()
    last_ts = _MEMORY_COOLDOWN.get(key, 0.0)

    # Check memory cache first
    if (now_ts - last_ts) < (cooldown_minutes * 60):
        return False

    # Check persistent DB state if available
    try:
        with db.connect() as conn:
            state_val = db.get_state(conn, f"alert_ts:{key}")
            if state_val:
                dt = datetime.fromisoformat(state_val)
                diff = (datetime.now(timezone.utc) - dt).total_seconds()
                if diff < (cooldown_minutes * 60):
                    _MEMORY_COOLDOWN[key] = dt.timestamp()
                    return False
    except Exception:
        # If DB is down, memory cache is used
        pass

    return True


def _record_alert(key: str) -> None:
    """Record that an alert was fired to enforce cooldown."""
    now_ts = time.time()
    _MEMORY_COOLDOWN[key] = now_ts
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with db.connect() as conn:
            db.set_state(conn, f"alert_ts:{key}", now_iso)
    except Exception:
        pass


def send_alert(key: str, message: str, cooldown_minutes: int = 30) -> bool:
    """Send an alert to Telegram if outside cooldown period."""
    if not _can_alert(key, cooldown_minutes):
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        # Telegram not configured; log to stderr
        print(f"⚠️ [ALERT NOT SENT - TELEGRAM UNCONFIGURED] [{key}]: {message}")
        _record_alert(key)
        return False

    ctx = os.getenv("GITHUB_WORKFLOW", "Local")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_str = f" (Run: #{run_id})" if run_id else ""
    full_text = f"🚨 *AGENT OUTREACH ALERT* [{ctx}{run_str}]\n\n{message}"

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": full_text[:4000], "parse_mode": "Markdown"},
            timeout=10
        )
        if resp.status_code == 200:
            _record_alert(key)
            return True
        else:
            # Fallback without markdown parsing if syntax error occurred
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": full_text[:4000]},
                timeout=10
            )
            _record_alert(key)
            return True
    except Exception as e:
        print(f"Failed to transmit Telegram alert: {e}")
        return False


def notify(message: str) -> bool:
    """Send an operational update or notification to Telegram (without alert cooldown)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print(f"ℹ️ [TELEGRAM UNCONFIGURED]: {message}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message[:4000], "parse_mode": "Markdown"},
            timeout=10
        )
        if resp.status_code == 200:
            return True
        # Fallback to plain text if markdown formatting failed
        resp2 = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": message[:4000]},
            timeout=10
        )
        return resp2.status_code == 200
    except Exception as e:
        print(f"Failed to transmit Telegram notification: {e}")
        return False


def is_notification_enabled(event_type: str) -> bool:
    """Check whether a notification event type should be sent to Telegram.
    
    Filters out background operational spam and preserves only SIGNIFICANT alerts:
    - hot_reply: True (interested, meeting_request, question, referral)
    - daily_cap_reached: True (milestone when 28 new quota is completed)
    - critical_alerts: True (safety pauses, auth failure, db error)
    - batch_send: False (suppress hourly dispatch ticks unless specifically enabled)
    - community_post: False (suppress scraping noise from Reddit/forums)
    - discovery: False (suppress background preparation noise)
    - dry_run: False (suppress simulation messages)
    """
    env_override = os.getenv(f"NOTIFY_{event_type.upper()}")
    if env_override is not None:
        return env_override.strip().lower() in ("true", "1", "yes")

    level = os.getenv("TELEGRAM_NOTIFY_LEVEL", "significant").strip().lower()
    if level in ("off", "none"):
        return False
    if level == "all":
        return True

    try:
        cfg = config.settings().get("notifications", {}).get("telegram", {})
        if cfg.get("enabled") is False:
            return False
        if f"on_{event_type}" in cfg:
            return bool(cfg[f"on_{event_type}"])
    except Exception:
        pass

    significant_defaults = {
        "critical_alerts": True,
        "hot_reply": True,
        "daily_cap_reached": True,
        "batch_send": False,
        "all_replies": False,
        "community_post": False,
        "discovery": False,
        "dry_run": False,
    }
    return significant_defaults.get(event_type, False)


def notify_send_summary(
    sent_count: int,
    total_today: int,
    ceiling: int = 28,
    freelance_count: int = 0,
    internship_count: int = 0,
    followup_count: int = 0,
    dry_run: bool = False,
    recipients: list[str] | None = None
) -> bool:
    """Notify Telegram only on significant send events: daily 28-cap milestone or if explicitly enabled."""
    if dry_run and not is_notification_enabled("dry_run"):
        return False
    if sent_count == 0:
        return False

    # 1. Milestone: Daily 28 new quota completed
    if total_today >= ceiling and is_notification_enabled("daily_cap_reached"):
        now_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _can_alert(f"cap_reached_{now_day}", cooldown_minutes=720):
            msg = (
                f"🎯 *DAILY OUTREACH QUOTA REACHED ({total_today}/{ceiling})*\n\n"
                f"• *Freelance New:* {freelance_count}\n"
                f"• *Internship New:* {internship_count}\n"
                f"• *Follow-ups Sent:* {followup_count} (unconstrained)\n\n"
                f"Daily limit of {ceiling} new contacts reached. The pipeline will hold new sends until tomorrow's window."
            )
            _record_alert(f"cap_reached_{now_day}")
            return notify(msg)

    # 2. Hourly/per-tick batch sends: Suppressed by default to prevent spamming
    if not is_notification_enabled("batch_send"):
        return False

    ctx = os.getenv("GITHUB_WORKFLOW", "Local")
    prefix = "🧪 *OUTREACH DRY-RUN REPORT*" if dry_run else "✉️ *OUTREACH DISPATCH REPORT*"
    remaining = max(0, ceiling - total_today)
    recip_text = ""
    if recipients:
        recip_text = "\n*Recipients:*\n" + "\n".join(f"• {r}" for r in recipients[:10])
    msg = (
        f"{prefix} [{ctx}]\n\n"
        f"• *Batch Dispatched:* {sent_count} email(s)\n"
        f"• *Freelance New:* {freelance_count}\n"
        f"• *Internship New:* {internship_count}\n"
        f"• *Follow-ups Sent:* {followup_count} (unconstrained)\n"
        f"• *Today's New Quota:* {total_today} / {ceiling} ({remaining} remaining)\n"
        f"{recip_text}"
    )
    return notify(msg)


def notify_reply_received(
    from_addr: str,
    company: str,
    category: str,
    summary: str,
    suggested_reply: str = "",
    lead_id: int | str = "?"
) -> bool:
    """Notify Telegram when a significant prospect reply arrives (interested, meeting, question, referral)."""
    is_hot = category in ("interested", "meeting_request", "question", "referral")
    if not (is_hot or is_notification_enabled("all_replies")):
        return False

    cat_emoji = "🔥" if category in ("interested", "meeting_request") else "💬"
    msg = (
        f"{cat_emoji} *PROSPECT REPLY: {category.upper()}*\n\n"
        f"• *Lead:* #{lead_id} {company or 'Company'}\n"
        f"• *From:* `{from_addr}`\n"
        f"• *Summary:* {summary}\n"
    )
    if suggested_reply:
        msg += f"\n*Drafted Answer Ready:*\n_{suggested_reply[:400]}_\n\n👉 Review & send in dashboard."
    return notify(msg)


def notify_discovery_complete(new_leads: int, ready_for_review: int, segments: list[str] | None = None) -> bool:
    """Notify Telegram when prospect discovery and drafting complete (suppressed by default)."""
    if not is_notification_enabled("discovery"):
        return False
    if new_leads == 0 and ready_for_review == 0:
        return False
    seg_text = f" across {', '.join(segments)}" if segments else ""
    msg = (
        f"🎯 *PIPELINE DISCOVERY & DRAFTING COMPLETE*\n\n"
        f"• *New Leads Researched:* {new_leads}{seg_text}\n"
        f"• *Drafts Waiting for Review:* {ready_for_review}\n"
        f"👉 Review them anytime on the dashboard."
    )
    return notify(msg)


def alert_db_unavailable(err: Exception | str) -> bool:
    return send_alert(
        "db_unavailable",
        f"🔥 *Remote Database Unavailable*\nError: `{str(err)[:200]}`\nCheck Turso URL & Auth Token.",
        cooldown_minutes=15
    )


def alert_provider_auth_failure(inbox: str, err: Exception | str) -> bool:
    return send_alert(
        f"auth_failure:{inbox}",
        f"🔑 *Provider Authentication Failure*\nInbox: `{inbox}`\nError: `{str(err)[:200]}`\nOutreach paused for this inbox.",
        cooldown_minutes=30
    )


def alert_bounce_threshold_exceeded(rate: float, total: int, threshold: float, inbox: str = "global") -> bool:
    return send_alert(
        f"bounce_exceeded:{inbox}",
        f"🛑 *Bounce Threshold Exceeded ({inbox})*\nRate: `{rate:.1%}` ({total} sends) exceeds limit `{threshold:.1%}`.\nOutbound sending has been automatically PAUSED to protect reputation.",
        cooldown_minutes=60
    )


def alert_duplicate_send_prevented(lead_id: int, step: int) -> bool:
    return send_alert(
        f"duplicate_prevented:{lead_id}:{step}",
        f"🛡️ *Duplicate Send Prevented*\nLead ID: `{lead_id}` | Step: `{step}`.\nAn attempt to send an already transmitted or claimed message was blocked by idempotency protection.",
        cooldown_minutes=30
    )


def alert_stuck_sending_state(stuck_count: int) -> bool:
    return send_alert(
        "stuck_sending",
        f"⚠️ *Stuck Sending State Detected*\n`{stuck_count}` message(s) were stuck in 'sending' status and transitioned to 'needs_reconciliation'. Verify Sent folder.",
        cooldown_minutes=30
    )


def alert_quota_integrity_failure(reason: str) -> bool:
    return send_alert(
        "quota_integrity",
        f"🛑 *Quota Integrity Failure*\nDetails: `{reason}`.\nExecution halted to prevent quota violation.",
        cooldown_minutes=30
    )
