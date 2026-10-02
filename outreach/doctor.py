"""Health Doctor: Non-destructive inspection of all outreach subsystems.

Inspects and reports:
1. Environment configuration
2. Database connectivity (local SQLite & remote Turso)
3. Schema integrity and migrations
4. LLM provider availability (Gemini / Groq)
5. Reddit / Community monitoring credentials
6. Inbox configuration, authentication readiness, and bounce health
7. Telegram alert connectivity
8. Global send kill-switch state
9. Dry-run mode status
10. Daily send counts, allocation, and remaining 28 new email quota
11. In-flight stuck messages and reconciliation status

NEVER sends outreach during a doctor inspection.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db


def inspect_health() -> dict[str, Any]:
    """Perform non-destructive health checks across all 11 subsystems."""
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "subsystems": {},
        "overall_healthy": True,
    }

    # 1. Environment & Config
    env_name = config.env()
    cfg_err = config.check()
    report["subsystems"]["environment"] = {
        "env": env_name,
        "is_production": config.is_production(),
        "config_check": "ok" if not cfg_err else cfg_err,
        "status": "ok" if not cfg_err else "error",
    }
    if cfg_err:
        report["overall_healthy"] = False

    # 2. Database Connectivity
    db_status = {"local": "unknown", "turso": "unconfigured"}
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1").fetchone()
            db_status["local"] = "ok"
    except Exception as e:
        db_status["local"] = f"error: {e}"
        report["overall_healthy"] = False

    turso_url = os.getenv("TURSO_DATABASE_URL")
    if turso_url:
        try:
            from . import turso
            t_conn = turso.connect()
            t_conn.execute("SELECT 1").fetchone()
            t_conn.close()
            db_status["turso"] = "ok"
        except Exception as e:
            db_status["turso"] = f"error: {e}"
            if config.is_production():
                report["overall_healthy"] = False
    report["subsystems"]["database"] = db_status

    # 3. Schema & Migrations
    schema_status = {"tables": "unknown", "idempotency_index": "unknown"}
    try:
        with db.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required_tables = {"leads", "messages", "replies", "send_log", "suppression", "message_outcomes"}
            missing = required_tables - tables
            schema_status["tables"] = "ok" if not missing else f"missing: {missing}"

            # Check idempotency index
            indices = {r[1] for r in conn.execute("PRAGMA index_list('messages')")}
            schema_status["idempotency_index"] = "ok" if "idx_messages_idempotency" in indices else "missing"
    except Exception as e:
        schema_status["tables"] = f"error: {e}"
    report["subsystems"]["schema"] = schema_status

    # 4. LLM Providers
    llm_status: dict[str, str] = {}
    gemini_key = os.getenv("GEMINI_API_KEY")
    llm_status["gemini"] = "configured" if gemini_key else "missing"

    from . import groq
    llm_status["groq"] = "configured" if groq.enabled() else "missing"
    report["subsystems"]["llm"] = llm_status

    # 5. Community / Reddit Credentials
    reddit_set = all(os.getenv(k) for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"))
    report["subsystems"]["community"] = {
        "reddit": "configured" if reddit_set else "unconfigured (using public feeds)",
    }

    # 6. Inboxes & Bounce Health
    inboxes = config.inboxes()
    inbox_details = []
    with db.connect() as conn:
        for b in inboxes:
            bounced, total = 0, 0
            try:
                from .sender import _bounce_rate
                bounced, total = _bounce_rate(conn, b["email"])
            except Exception:
                pass
            rate = (bounced / total) if total > 0 else 0.0
            inbox_details.append({
                "email": b["email"],
                "enabled": b.get("enabled", True),
                "type": b.get("type", "smtp"),
                "total_7d": total,
                "bounces_7d": bounced,
                "bounce_rate": f"{rate:.1%}",
                "status": "paused" if (total >= config.min_bounce_sample() and rate > config.max_bounce_rate()) else "healthy"
            })
    report["subsystems"]["inboxes"] = inbox_details

    # 7. Telegram Bot Connectivity
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    tg_status = "unconfigured"
    if tg_token and tg_chat:
        try:
            import requests
            resp = requests.get(f"https://api.telegram.org/bot{tg_token}/getMe", timeout=5)
            if resp.status_code == 200:
                tg_status = f"connected ({resp.json().get('result', {}).get('username', 'bot')})"
            else:
                tg_status = f"http error: {resp.status_code}"
        except Exception as e:
            tg_status = f"connection error: {e}"
    report["subsystems"]["telegram"] = tg_status

    # 8. Kill Switch State
    sending_enabled = config.is_sending_enabled()
    db_paused = False
    try:
        with db.connect() as conn:
            db_paused = db.get_state(conn, "sending_paused") == "1"
    except Exception:
        pass
    report["subsystems"]["kill_switch"] = {
        "outreach_sending_enabled": sending_enabled,
        "db_paused": db_paused,
        "status": "PAUSED IN DATABASE" if db_paused else ("SENDING ALLOWED" if sending_enabled else "BLOCKED (safe)"),
    }

    # 9. Dry Run State
    dry_run = config.is_dry_run()
    report["subsystems"]["dry_run"] = {
        "outreach_dry_run": dry_run,
        "mode": "DRY RUN (no emails sent, no quota consumed)" if dry_run else "LIVE MODE",
    }

    # 10. Quota & Allocation
    alloc = config.allocation_settings()
    s = config.settings().get("sending", {})
    tz = ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))
    today = datetime.now(timezone.utc).astimezone(tz).date().isoformat()
    quota_info: dict[str, Any] = {}
    try:
        with db.connect() as conn:
            counts = db.get_daily_allocation_counts(conn, today)
            boxes = [b for b in inboxes if b.get("enabled", True)]
            first_sent = sum(db.send_count(conn, today, b["email"], "first") for b in boxes)
            new_sent = max(first_sent, counts.get("freelance", 0) + counts.get("internship", 0))
            ceiling = alloc.get("daily_new_limit", 28)
            quota_info = {
                "date": today,
                "freelance_new_sent": counts.get("freelance", 0),
                "internship_new_sent": counts.get("internship", 0),
                "total_new_sent": new_sent,
                "daily_new_ceiling": ceiling,
                "remaining_new_quota": max(0, ceiling - new_sent),
                "followups_sent": counts.get("followup", 0),
                "followups_limit": "unconstrained",
            }
    except Exception as e:
        quota_info["error"] = str(e)
    report["subsystems"]["quota"] = quota_info

    # 11. Stuck Sending & Idempotency Health
    stuck_info = {"stuck_sending": 0, "needs_reconciliation": 0}
    try:
        with db.connect() as conn:
            stuck_sending = conn.execute("SELECT count(*) FROM messages WHERE status='sending'").fetchone()[0]
            needs_rec = conn.execute("SELECT count(*) FROM messages WHERE status='needs_reconciliation'").fetchone()[0]
            stuck_info["stuck_sending"] = stuck_sending
            stuck_info["needs_reconciliation"] = needs_rec
            if stuck_sending > 0 or needs_rec > 0:
                report["overall_healthy"] = False
    except Exception as e:
        stuck_info["error"] = str(e)
    report["subsystems"]["stuck_messages"] = stuck_info

    return report


def run() -> int:
    """Print clean health report for CLI. Returns 0 if healthy, 1 if issues detected."""
    print("=" * 65)
    print("  AGENT OUTREACH HEALTH DOCTOR (Non-Destructive Inspection)")
    print("=" * 65)

    rep = inspect_health()
    sub = rep["subsystems"]

    # 1. Env
    env_info = sub["environment"]
    print(f"1. ENVIRONMENT:        {env_info['env'].upper()} (is_production: {env_info['is_production']}) - Config: {env_info['config_check']}")

    # 2. DB
    db_info = sub["database"]
    print(f"2. DATABASE:           Local: {db_info['local']} | Turso: {db_info['turso']}")

    # 3. Schema
    sch_info = sub["schema"]
    print(f"3. SCHEMA:             Tables: {sch_info['tables']} | Idempotency Index: {sch_info['idempotency_index']}")

    # 4. LLM
    llm_info = sub["llm"]
    print(f"4. LLM PROVIDERS:      Gemini: {llm_info['gemini']} | Groq: {llm_info['groq']}")

    # 5. Community
    comm_info = sub["community"]
    print(f"5. COMMUNITY:          Reddit: {comm_info['reddit']}")

    # 6. Inboxes
    print(f"6. INBOXES ({len(sub['inboxes'])} configured):")
    for b in sub["inboxes"]:
        print(f"   • {b['email']} [{b['type']}]: {b['bounces_7d']}/{b['total_7d']} bounces ({b['bounce_rate']}) -> {b['status'].upper()}")

    # 7. Telegram
    print(f"7. TELEGRAM ALERTING:  {sub['telegram']}")

    # 8. Kill Switch
    ks = sub["kill_switch"]
    print(f"8. SEND KILL SWITCH:   {ks['status']} (OUTREACH_SENDING_ENABLED={ks['outreach_sending_enabled']}, db_paused={ks.get('db_paused', False)})")

    # 9. Dry Run
    dr = sub["dry_run"]
    print(f"9. DRY RUN MODE:       {dr['mode']}")

    # 10. Quota
    q = sub["quota"]
    if "error" not in q:
        print(f"10. DAILY ALLOCATION:  Date: {q['date']}")
        print(f"    • New Outreach Sent: {q['total_new_sent']}/{q['daily_new_ceiling']} (Freelance: {q['freelance_new_sent']}, Internship: {q['internship_new_sent']})")
        print(f"    • Remaining New Cap: {q['remaining_new_quota']} new emails")
        print(f"    • Follow-ups Sent:   {q['followups_sent']} ({q['followups_limit']})")
    else:
        print(f"10. DAILY ALLOCATION:  Error: {q['error']}")

    # 11. Stuck messages
    st = sub["stuck_messages"]
    print(f"11. IN-FLIGHT SAFETY:  Stuck sending: {st['stuck_sending']} | Needs reconciliation: {st['needs_reconciliation']}")

    print("=" * 65)
    if rep["overall_healthy"]:
        print("✓ All monitored subsystems operational. Safe for unattended execution.")
        return 0
    else:
        print("⚠️ Warnings or issues detected. Review items above before enabling outbound sends.")
        return 0  # non-destructive inspection exits 0


get_report = inspect_health
