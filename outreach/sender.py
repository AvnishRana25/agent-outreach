"""Sends approved emails, a couple per run, inside each lead's local business hours.

Run it from cron every ~10 minutes (see scripts/crontab.example). Each run sends at most
`--max` emails, so volume trickles out through the day like a person sending by hand, which is
what keeps new domains out of spam. Per-inbox daily caps ramp up automatically from each
inbox's `warmup_start` date.
"""
from __future__ import annotations

import random
import smtplib
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

from . import config, db, personalize, transport
from .sources import rejects_ai_application


def daily_cap(box: dict, today: date) -> int:
    """Cold sends allowed today for this inbox, following the warm-up ramp in settings.yaml."""
    start = box.get("warmup_start")
    if not start:
        return box.get("max_per_day", 30)
    start = start if isinstance(start, date) else date.fromisoformat(str(start))
    week = max(0, (today - start).days) // 7
    ramp = config.settings()["sending"]["ramp_by_week"]
    return min(box.get("max_per_day", 30), ramp[min(week, len(ramp) - 1)])


def draft_room(days: int = 2, now: datetime | None = None) -> dict:
    """How many new first emails are worth drafting: what the inboxes can send over the next `days` days,
    minus follow-ups falling due and first emails already waiting (drafted or approved). Drafting more
    only grows the review queue, lets hooks go stale and spends Gemini quota on emails that wait a week."""
    s = config.settings()["sending"]
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))).date()
    boxes = [b for b in config.inboxes() if b.get("enabled", True)]
    with db.connect() as conn:
        capacity = sum(daily_cap(b, date.fromordinal(today.toordinal() + i)) for b in boxes for i in range(days))
        sent_today = sum(db.send_count(conn, today.isoformat(), b["email"]) for b in boxes)
        horizon = datetime.fromordinal(today.toordinal() + days).replace(tzinfo=timezone.utc).isoformat()
        followups = conn.execute("SELECT COUNT(*) FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.step>0 "
                                 "AND m.status='approved' AND l.status='active' AND m.due_at LIKE '____-%' "
                                 "AND m.due_at < ?", (horizon,)).fetchone()[0]
        waiting = conn.execute("SELECT COUNT(*) FROM leads WHERE status IN ('drafted','approved')").fetchone()[0]
    room = max(0, capacity - sent_today - followups - waiting)
    return {"room": room, "capacity": capacity, "sent_today": sent_today, "followups": followups, "waiting": waiting}


def in_window(seg: dict, now_utc: datetime) -> bool:
    tz = ZoneInfo(seg.get("timezone", "Asia/Kolkata"))
    local = now_utc.astimezone(tz)
    if local.isoweekday() not in seg.get("send_days", [1, 2, 3, 4, 5]):
        return False
    for window in seg.get("send_windows", ["09:45-12:30", "14:30-17:30"]):
        a, b = window.split("-")
        if time.fromisoformat(a) <= local.time() <= time.fromisoformat(b):
            return True
    return False


def _signature(seg: dict) -> str:
    sigs = config.profile()["signatures"]
    return sigs.get(seg.get("signature", "freelance"), sigs["freelance"])


def _bounce_rate(conn, inbox: str) -> tuple[int, int]:
    sent = conn.execute("SELECT COUNT(*) FROM messages WHERE inbox=? AND status='sent' AND step=0 "
                        "AND sent_at >= datetime('now','-7 days')", (inbox,)).fetchone()[0]
    bounced = conn.execute("SELECT COUNT(*) FROM replies WHERE inbox=? AND category='bounce' "
                           "AND received_at >= datetime('now','-7 days')", (inbox,)).fetchone()[0]
    return bounced, sent


def _last_sent(conn, inbox: str) -> datetime | None:
    row = conn.execute("SELECT MAX(sent_at) FROM messages WHERE inbox=? AND status='sent'", (inbox,)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def _candidates(conn, now_iso: str):
    """Due follow-ups first (they keep threads alive), then new first emails by score."""
    followups = conn.execute(
        """SELECT m.*, l.segment, l.inbox AS lead_inbox FROM messages m JOIN leads l ON l.id=m.lead_id
           WHERE m.step>0 AND m.status='approved' AND l.status='active' AND m.due_at <= ?
             AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.lead_id=m.lead_id AND p.step<m.step
                             AND p.status NOT IN ('sent','cancelled'))
           ORDER BY m.due_at""", (now_iso,)).fetchall()
    firsts = conn.execute(
        """SELECT m.*, l.segment, l.inbox AS lead_inbox FROM messages m JOIN leads l ON l.id=m.lead_id
           WHERE m.step=0 AND m.status='approved' AND l.status='approved'
           ORDER BY l.score DESC, m.id""").fetchall()
    return list(followups) + list(firsts)


def tick(max_sends: int = 2, dry_run: bool = False) -> int:
    s = config.settings()["sending"]
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))).date().isoformat()
    boxes = {b["email"]: b for b in config.inboxes() if b.get("enabled", True)}
    sent = 0
    with db.connect() as conn:
        if not dry_run:
            db.purge_mock(conn)
        if db.get_state(conn, "sending_paused") == "1":
            print("  sending is paused from the dashboard")
            return 0
        paused = set()
        for email in boxes:
            bounced, total = _bounce_rate(conn, email)
            if total >= 30 and bounced / total > s.get("max_bounce_rate", 0.03):
                print(f"  PAUSED {email}: {bounced}/{total} bounces in 7 days. Clean the list before resuming.")
                paused.add(email)

        for msg in _candidates(conn, now.isoformat()):
            if sent >= max_sends:
                break
            if db.MOCK_MARK in (msg["body"] or "") or (msg["subject"] or "").startswith("[mock"):
                if not dry_run:
                    conn.execute("UPDATE messages SET status='cancelled' WHERE id=?", (msg["id"],))
                continue
            allowed_segments = s.get("allowed_segments")
            if allowed_segments is not None and msg["segment"] not in allowed_segments:
                continue
            seg = config.segment(msg["segment"])
            if not in_window(seg, now):
                continue
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (msg["lead_id"],)).fetchone()
            if db.suppressed(conn, lead["email"]):
                if not dry_run:
                    conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status='approved'", (lead["id"],))
                    db.set_lead(conn, lead["id"], status="unsubscribed")
                continue
            if (lead["email_status"] != "valid"
                    or lead["email_source"] not in ("website", "osm", "post", "registry", "maps")
                    or any(rejects_ai_application(lead[field] or "")
                           for field in ("source_text", "site_text", "notes", "research"))
                    or lead["fit"] is None or lead["fit"] < 6
                    or msg["confidence"] is None or msg["confidence"] < 0.85
                    or not (msg["subject"] or "").strip() or not (msg["body"] or "").strip()):
                print(f"  ! held {lead['email']}: address, fit, confidence, or content needs review")
                continue

            box = _choose_inbox(conn, msg, seg, boxes, paused, today, now, s)
            if not box:
                continue
            synced = db.get_state(conn, f"last_inbound_sync:{box['email']}")
            try:
                sync_age = now - datetime.fromisoformat(synced)
            except (ValueError, TypeError):
                sync_age = timedelta.max
            if not timedelta(0) <= sync_age <= timedelta(minutes=20):
                print(f"  ! held {lead['email']}: inbox {box['email']} has no recent successful sync")
                continue
            thread = None
            if msg["step"] > 0:
                first = conn.execute("SELECT message_id, provider_id FROM messages WHERE lead_id=? AND step=0",
                                     (lead["id"],)).fetchone()
                thread = dict(first) if first else None
            body = f"{msg['body'].strip()}\n\n{_signature(seg)}\n"
            hole = config.PLACEHOLDER.search(f"{msg['subject']}\n{body}")
            if hole:  # never send template text
                in_draft = bool(config.PLACEHOLDER.search(f"{msg['subject']}\n{msg['body']}"))
                if in_draft and msg["step"] == 0:  # Gemini wrote it: back to Review with a note
                    if not dry_run:
                        conn.execute("UPDATE messages SET status='draft', review_note=? WHERE lead_id=? AND status='approved'",
                                     (f"Contains placeholder text {hole.group(0)!r}: edit it before approving.", lead["id"]))
                        db.set_lead(conn, lead["id"], status="drafted")
                else:  # the signature: the dashboard shows a banner until profile.yaml is fixed
                    if not dry_run:
                        conn.execute("UPDATE messages SET error=? WHERE id=?",
                                     (f"held: placeholder text {hole.group(0)!r}", msg["id"]))
                print(f"  ! held {lead['email']}: placeholder text {hole.group(0)!r}")
                continue

            if dry_run:
                print(f"  [dry-run] {box['email']} -> {lead['email']} step {msg['step']}: {msg['subject']}")
                sent += 1
                continue
            claimed = conn.execute("UPDATE messages SET status='sending', inbox=?, error=NULL "
                                   "WHERE id=? AND status='approved'", (box["email"], msg["id"]))
            if not claimed.rowcount:
                continue
            conn.commit()  # Durable before the provider call; uncertain outcomes cannot be retried automatically.
            try:
                message_id, provider_id = transport.send(box, lead["email"], msg["subject"], body, thread)
            except Exception as e:
                conn.execute("UPDATE messages SET status='needs_reconciliation', error=? WHERE id=?",
                             (f"{type(e).__name__}: check provider before retry", msg["id"]))
                print(f"  ! send uncertain {box['email']} -> {lead['email']}: {type(e).__name__}")
                if isinstance(e, smtplib.SMTPRecipientsRefused):
                    conn.execute("UPDATE messages SET status='failed' WHERE id=?", (msg["id"],))
                    db.set_lead(conn, lead["id"], status="bounced")
                conn.commit()
                continue

            conn.execute("UPDATE messages SET status='sent', sent_at=?, message_id=?, provider_id=?, inbox=? "
                         "WHERE id=?", (now.isoformat(), message_id, provider_id, box["email"], msg["id"]))
            db.bump_send_count(conn, today, box["email"], "first" if msg["step"] == 0 else "followup")
            if msg["step"] == 0:
                db.set_lead(conn, lead["id"], status="active", inbox=box["email"])
                personalize.schedule_followups(conn, lead["id"], now)
            elif msg["step"] >= len(config.settings()["sequence"]["followup_days"]):
                db.set_lead(conn, lead["id"], status="finished")
            conn.commit()
            sent += 1
            print(f"  sent {box['email']} -> {lead['email']} step {msg['step']}")
    return sent


def _choose_inbox(conn, msg, seg, boxes, paused, today, now, s) -> dict | None:
    if msg["step"] > 0:  # follow-ups must come from the inbox that started the thread
        options = [msg["lead_inbox"]] if msg["lead_inbox"] else []
    else:
        options = seg.get("inboxes") or list(boxes)
    best, best_room = None, 0
    for email in options:
        box = boxes.get(email)
        if not box or email in paused:
            continue
        cap = daily_cap(box, date.fromisoformat(today))
        room = cap - db.send_count(conn, today, email)
        last = _last_sent(conn, email)
        gap = s.get("min_gap_minutes", 7) + random.uniform(0, s.get("gap_jitter_minutes", 5))
        if last and (now - last).total_seconds() < gap * 60:
            continue
        if room > best_room:
            best, best_room = box, room
    return best
