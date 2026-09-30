"""Sends approved emails, a couple per run, inside each lead's local business hours.

Run it from cron every ~10 minutes (see scripts/crontab.example). Each run sends at most
`--max` emails, so volume trickles out through the day like a person sending by hand, which is
what keeps new domains out of spam. Per-inbox daily caps ramp up automatically from each
inbox's `warmup_start` date.
"""
from __future__ import annotations

import random
import smtplib
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from . import config, db, personalize, transport


def daily_cap(box: dict, today: date) -> int:
    """Cold sends allowed today for this inbox, following the warm-up ramp in settings.yaml."""
    start = box.get("warmup_start")
    if not start:
        return box.get("max_per_day", 30)
    start = start if isinstance(start, date) else date.fromisoformat(str(start))
    week = max(0, (today - start).days) // 7
    ramp = config.settings()["sending"]["ramp_by_week"]
    return min(box.get("max_per_day", 30), ramp[min(week, len(ramp) - 1)])


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
                conn.execute("UPDATE messages SET status='cancelled' WHERE id=?", (msg["id"],))
                continue
            seg = config.segment(msg["segment"])
            if not in_window(seg, now):
                continue
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (msg["lead_id"],)).fetchone()
            if db.suppressed(conn, lead["email"]):
                conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status='approved'", (lead["id"],))
                db.set_lead(conn, lead["id"], status="unsubscribed")
                continue

            box = _choose_inbox(conn, msg, seg, boxes, paused, today, now, s)
            if not box:
                continue
            thread = None
            if msg["step"] > 0:
                first = conn.execute("SELECT message_id, provider_id FROM messages WHERE lead_id=? AND step=0",
                                     (lead["id"],)).fetchone()
                thread = dict(first) if first else None
            body = f"{msg['body'].strip()}\n\n{_signature(seg)}\n"

            if dry_run:
                print(f"  [dry-run] {box['email']} -> {lead['email']} step {msg['step']}: {msg['subject']}")
                sent += 1
                continue
            try:
                message_id, provider_id = transport.send(box, lead["email"], msg["subject"], body, thread)
            except transport.SEND_ERRORS as e:
                conn.execute("UPDATE messages SET error=? WHERE id=?", (str(e)[:300], msg["id"]))
                print(f"  ! send failed {box['email']} -> {lead['email']}: {e}")
                if isinstance(e, smtplib.SMTPRecipientsRefused):
                    conn.execute("UPDATE messages SET status='failed' WHERE id=?", (msg["id"],))
                    db.set_lead(conn, lead["id"], status="bounced")
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
