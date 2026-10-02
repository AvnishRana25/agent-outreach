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

from . import config, db, personalize, replies, transport
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
        sources = ",".join("?" * len(TRUSTED_SOURCES))
        waiting = conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE status IN ('drafted','approved') AND fit>=? "
            f"AND email_status='valid' AND email_source IN ({sources})",
            (config.settings().get("targeting", {}).get("min_fit", 6), *TRUSTED_SOURCES)).fetchone()[0]
        first_sent_today = sum(db.send_count(conn, today.isoformat(), b["email"], "first") for b in boxes)
        first_target = int(s.get("daily_first_target", 28))
        room = max(0, min(capacity - sent_today - followups - waiting,
                          first_target * days - first_sent_today - waiting))
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
    """Due follow-ups first (they keep threads alive), then new first emails deterministically ranked by score band."""
    followups = conn.execute(
        """SELECT m.*, l.email, l.segment, l.inbox AS lead_inbox FROM messages m JOIN leads l ON l.id=m.lead_id
           WHERE m.step>0 AND m.status='approved' AND l.status='active' AND m.due_at <= ?
             AND NOT EXISTS (SELECT 1 FROM messages p WHERE p.lead_id=m.lead_id AND p.step<m.step
                             AND p.status NOT IN ('sent','cancelled'))
           ORDER BY m.due_at""", (now_iso,)).fetchall()
    firsts = conn.execute(
        """SELECT m.*, l.email, l.segment, l.inbox AS lead_inbox,
                  l.score_total, l.score, l.fit, l.created_at, l.email_status, l.email_source
           FROM messages m JOIN leads l ON l.id=m.lead_id
           WHERE m.step=0 AND m.status='approved' AND l.status='approved'
           ORDER BY
             CASE
               WHEN COALESCE(l.score_total, l.score, 0) >= 85 THEN 0
               WHEN COALESCE(l.score_total, l.score, 0) >= 75 THEN 1
               WHEN COALESCE(l.score_total, l.score, 0) >= 60 THEN 2
               ELSE 3
             END,
             COALESCE(l.score_total, l.score, 0) DESC,
             COALESCE(m.confidence, 0.0) DESC,
             COALESCE(l.fit, 0) DESC,
             CASE WHEN l.email_status = 'valid' THEN 0 WHEN l.email_status = 'risky' THEN 1 ELSE 2 END,
             CASE WHEN l.email_source IN ('website', 'public_match', 'provider_verified') THEN 0 ELSE 1 END,
             COALESCE(l.created_at, '') DESC,
             m.id ASC""").fetchall()
    return list(followups) + list(firsts)


# provider_verified: an email finder confirmed the mailbox (see verify.confirm_guesses)
TRUSTED_SOURCES = ("website", "osm", "post", "registry", "maps", "provider_verified", "public_match", "prospeo", "hunter", "skrapp")
INBOX_FRESH = timedelta(minutes=30)   # the inbox job runs every 10 minutes; replies must be read before sending
MAX_ATTEMPTS = 3                       # provider rejections before an email is held for you to look at


def hold_reason(lead, msg, s: dict) -> str:
    """Why this email must wait for you, in plain words; '' when it may go. You see the reason in the
    dashboard's Review tab and can press "Send anyway" for the ones marked overridable there."""
    override = bool(msg["override"])
    by_you = msg["approved_by"] != "auto"
    allowed = s.get("allowed_segments")
    if allowed is not None and lead["segment"] not in allowed:
        return f"segment {lead['segment']} is switched off in settings.yaml (sending: allowed_segments)"
    if not (msg["subject"] or "").strip() or not (msg["body"] or "").strip():
        return "the subject or body is empty"
    from . import eligibility
    valid_content, content_reason = eligibility.validate_message_content(lead, msg)
    if not valid_content:
        return content_reason
    if any(rejects_ai_application(lead[f] or "") for f in ("source_text", "site_text", "notes", "research")):
        return "their post or site says AI-written applications are rejected; reply by hand instead"
    if lead["email_status"] not in ("valid", "risky"):
        return f"the address is {lead['email_status'] or 'unchecked'}, so it may bounce"
    if (msg["attempts"] or 0) >= MAX_ATTEMPTS:
        return f"the mail provider refused it {msg['attempts']} times: {msg['error'] or 'no details'}"
    min_fit = config.settings().get("targeting", {}).get("min_fit", 6)
    min_score = config.settings().get("quality", {}).get("min_opportunity_score", 75)
    score_val = lead["score_total"] if "score_total" in lead.keys() and lead["score_total"] else None
    if msg["step"] == 0:
        if score_val is not None and score_val > 0 and score_val < min_score:
            return f"opportunity score is {score_val}/100 (needs {min_score})"
        if lead["fit"] is None or lead["fit"] < min_fit:
            return f"research fit is {lead['fit'] if lead['fit'] is not None else 'unknown'}/10 (needs {min_fit})"
        if lead["email_status"] != "valid":
            return "first email needs a valid address, not a risky role address"
        if lead["email_source"] not in TRUSTED_SOURCES:
            return "first email needs a public or provider-verified address"
    if override:
        return ""
    if lead["email_status"] == "risky" and not by_you:
        return "generic address (info@/hello@): auto-approval only sends to named people"
    if not by_you and (msg["confidence"] is None or msg["confidence"] < 0.85):
        return "auto-approved, but the AI was less than 85% sure of the draft"
    return ""


def _hold(conn, msg, reason: str, dry_run: bool) -> None:
    if not dry_run and (msg["hold"] or "") != reason:
        conn.execute("UPDATE messages SET hold=? WHERE id=?", (reason, msg["id"]))


def inbox_fresh(conn, email: str, now: datetime) -> bool:
    try:
        age = now - datetime.fromisoformat(db.get_state(conn, f"last_inbound_sync:{email}"))
    except (ValueError, TypeError):
        return False
    return timedelta(0) <= age <= INBOX_FRESH


def reconcile_stale_sending(conn, max_age_minutes: int = 15) -> int:
    """If a process crashed or runner terminated while in-flight, move stuck 'sending'
    messages to 'needs_reconciliation' so subsequent runs never blindly duplicate outreach."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    rows = conn.execute(
        "SELECT id, lead_id, step FROM messages WHERE status='sending' "
        "AND (sending_at IS NULL OR sending_at = '' OR sending_at < ?)",
        (cutoff,)
    ).fetchall()
    for r in rows:
        err = "Send timed out or process terminated while sending. Check Sent folder before retrying."
        conn.execute("UPDATE messages SET status='needs_reconciliation', error=? WHERE id=?", (err, r["id"]))
    if rows:
        conn.commit()
    return len(rows)


def tick(max_sends: int = 2, dry_run: bool = False) -> int:
    s = config.settings()["sending"]
    now = datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))).date().isoformat()
    boxes = {b["email"]: b for b in config.inboxes() if b.get("enabled", True)}
    sent = 0

    dry_run = dry_run or config.is_dry_run()

    if not dry_run and not config.is_sending_enabled():
        from . import logging as outreach_logging
        outreach_logging.log_event("sender.tick", "blocked", error_message="Outbound sending blocked: OUTREACH_SENDING_ENABLED=false (fail-safe kill-switch engaged)", level="WARNING")
        print("  sending is disabled via OUTREACH_SENDING_ENABLED=false (fail-safe kill-switch)")
        return 0

    with db.connect() as conn:
        reconcile_stale_sending(conn)
        if not dry_run:
            db.purge_mock(conn)
        if db.get_state(conn, "sending_paused") == "1":
            print("  sending is paused from the dashboard")
            return 0

        # Bounce rate protection
        min_sample = config.min_bounce_sample()
        max_rate = config.max_bounce_rate()
        paused = set()
        for email in boxes:
            bounced, total = _bounce_rate(conn, email)
            rate = (bounced / total) if total > 0 else 0.0
            if total >= min_sample and rate > max_rate:
                print(f"  PAUSED {email}: {bounced}/{total} ({rate:.1%}) bounces in 7 days (limit {max_rate:.1%}). Clean the list before resuming.")
                paused.add(email)
                try:
                    from . import alerts
                    alerts.alert_bounce_threshold_exceeded(rate=rate, total=total, threshold=max_rate, inbox=email)
                except Exception:
                    pass

        if len(paused) == len(boxes) and len(boxes) > 0:
            print("  ALL inboxes paused due to bounce rate safety threshold. Outbound sending halted.")
            return 0

        # Allocation & quota
        alloc = config.allocation_settings()
        daily_new_limit = alloc.get("daily_new_limit", alloc.get("daily_send_limit", 28))
        first_sent_today = sum(db.send_count(conn, today, email, "first") for email in boxes)
        counts = db.get_daily_allocation_counts(conn, today)
        new_sent_today = max(first_sent_today, counts.get("freelance", 0) + counts.get("internship", 0))

        for msg in _candidates(conn, now.isoformat()):
            if sent >= max_sends:
                break

            # The 28 ceiling applies strictly to NEW initial outreach (step == 0).
            # Follow-ups (step > 0) are UNCONSTRAINED and can proceed even if 28 new emails reached today!
            if msg["step"] == 0 and new_sent_today >= daily_new_limit:
                continue
            if db.MOCK_MARK in (msg["body"] or "") or (msg["subject"] or "").startswith("[mock"):
                if not dry_run:
                    conn.execute("UPDATE messages SET status='cancelled' WHERE id=?", (msg["id"],))
                continue
            lead = conn.execute("SELECT * FROM leads WHERE id=?", (msg["lead_id"],)).fetchone()
            from . import eligibility
            elig = eligibility.evaluate_send_eligibility(lead, msg, conn, now=now)
            if not elig.eligible:
                if "suppression" in elig.failed_gates:
                    if not dry_run:
                        conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status='approved'", (lead["id"],))
                        db.set_lead(conn, lead["id"], status="unsubscribed")
                    continue
                if "contact_history" in elig.failed_gates:
                    if not dry_run:
                        conn.execute("UPDATE messages SET status='cancelled', error=? WHERE id=?", (elig.summary, msg["id"]))
                    continue
                if "content_integrity" in elig.failed_gates:
                    if not dry_run:
                        hole = config.PLACEHOLDER.search(f"{msg['subject']}\n{msg['body']}")
                        if hole and msg["step"] == 0:
                            conn.execute("UPDATE messages SET status='draft', review_note=? WHERE lead_id=? AND status='approved'",
                                         (f"Contains placeholder text {hole.group(0)!r}: edit it before approving.", lead["id"]))
                            db.set_lead(conn, lead["id"], status="drafted")
                        else:
                            conn.execute("UPDATE messages SET status='cancelled', error=? WHERE id=?", (elig.summary, msg["id"]))
                    continue
                if any(g in elig.failed_gates for g in ("quota", "timing", "sending_window", "timing_delay")):
                    continue
                reason = hold_reason(lead, msg, s) or elig.summary
                _hold(conn, msg, reason, dry_run)
                continue

            reason = hold_reason(lead, msg, s)
            _hold(conn, msg, reason, dry_run)
            if reason:
                continue
            seg = config.segment(msg["segment"])
            if not in_window(seg, now):
                continue

            box = _choose_inbox(conn, msg, seg, boxes, paused, today, now, s)
            if not box:
                continue
            if not inbox_fresh(conn, box["email"], now):  # a reply may be waiting: read it first
                print(f"  waiting: {box['email']} hasn't been read in the last {INBOX_FRESH.seconds // 60} min")
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
                from . import logging as outreach_logging
                from .scoring import get_opportunity_mode
                mode = get_opportunity_mode(lead)
                print(f"  [dry-run] {box['email']} -> {lead['email']} step {msg['step']} [{mode}]: {msg['subject']}")
                outreach_logging.log_event(
                    "outreach.dry_run",
                    "simulated",
                    lead_id=lead["id"],
                    message_id=msg["id"],
                    mode=mode,
                    inbox=box["email"],
                    recipient=lead["email"],
                    step=msg["step"],
                    subject=msg["subject"]
                )
                sent += 1
                continue
            conn.commit()  # nothing may hold the database while we talk to the mail provider
            try:  # log in first: a failure here certainly sent nothing
                transport.ready(box)
            except Exception as e:
                print(f"  ! can't send from {box['email']} right now: {type(e).__name__}: {e}")
                with_error = f"not sent yet: {type(e).__name__}: {e}"[:300]
                conn.execute("UPDATE messages SET error=? WHERE id=?", (with_error, msg["id"]))
                try:
                    from . import alerts
                    alerts.alert_provider_auth_failure(box["email"], e)
                except Exception:
                    pass
                break
            now_iso = now.isoformat()
            claimed = conn.execute("UPDATE messages SET status='sending', inbox=?, sending_at=?, error=NULL, hold='' "
                                   "WHERE id=? AND status='approved'", (box["email"], now_iso, msg["id"]))
            if not claimed.rowcount:
                continue
            conn.commit()  # durable before the provider call, so a crash mid-send can't send twice

            # Ensure duplicate sequence step was not already sent for this lead
            already = conn.execute(
                "SELECT 1 FROM messages WHERE lead_id=? AND step=? AND status='sent' AND id!=?",
                (msg["lead_id"], msg["step"], msg["id"])
            ).fetchone()
            if already:
                conn.execute("UPDATE messages SET status='cancelled', error='sequence step already sent' WHERE id=?", (msg["id"],))
                conn.commit()
                try:
                    from . import alerts
                    alerts.alert_duplicate_send_prevented(msg["lead_id"], msg["step"])
                except Exception:
                    pass
                continue

            custom_mid = f"outreach-{lead['id']}-step-{msg['step']}-{int(now.timestamp())}"
            import time as _pytime
            from . import retries
            from . import logging as outreach_logging
            send_start = _pytime.perf_counter()

            def _do_send():
                try:
                    return transport.send(box, lead["email"], msg["subject"], body, thread,
                                          custom_msg_id=custom_mid)
                except TypeError:
                    return transport.send(box, lead["email"], msg["subject"], body, thread)

            try:
                message_id, provider_id = retries.with_retry(
                    _do_send,
                    max_retries=3,
                    initial_delay=0.5,
                    backoff_factor=2.0,
                    operation_name=f"send_email:{lead['email']}"
                )
            except Exception as e:
                dur_ms = (_pytime.perf_counter() - send_start) * 1000.0
                what = f"{type(e).__name__}: {e}"[:300]
                outreach_logging.log_event(
                    "outreach.send",
                    "failed",
                    lead_id=lead["id"],
                    message_id=msg["id"],
                    duration_ms=dur_ms,
                    error_type=type(e).__name__,
                    error_message=what,
                    level="ERROR"
                )
                if isinstance(e, smtplib.SMTPRecipientsRefused):
                    conn.execute("UPDATE messages SET status='failed', error=? WHERE id=?", (what, msg["id"]))
                    db.set_lead(conn, lead["id"], status="bounced")
                    db.update_message_outcome(conn, lead["id"], outcome="bounced", notes=what, message_id=msg["id"])
                elif transport.not_sent(e):  # the provider refused it: safe to try again later
                    tries = (msg["attempts"] or 0) + 1
                    conn.execute("UPDATE messages SET status='approved', attempts=?, error=?, hold=? WHERE id=?",
                                 (tries, what, f"the mail provider refused it {tries} times: {what}"
                                  if tries >= MAX_ATTEMPTS else "", msg["id"]))
                    print(f"  ! not sent {box['email']} -> {lead['email']}: {what}")
                else:  # timed out or dropped after the request left: it may have gone out
                    conn.execute("UPDATE messages SET status='needs_reconciliation', error=? WHERE id=?",
                                 (f"{what}. Check your Sent folder, then mark it in the dashboard.", msg["id"]))
                    print(f"  ! send uncertain {box['email']} -> {lead['email']}: {type(e).__name__}")
                    conn.commit()  # save it before the Telegram call
                    replies.notify(f"⚠️ Not sure an email to {lead['email']} went out ({type(e).__name__}). "
                                   "Check Sent in Zoho, then mark it in the dashboard (Review → Needs a decision).")
                conn.commit()
                continue

            dur_ms = (_pytime.perf_counter() - send_start) * 1000.0
            conn.execute("UPDATE messages SET status='sent', sent_at=?, message_id=?, provider_id=?, inbox=? "
                         "WHERE id=?", (now.isoformat(), message_id, provider_id, box["email"], msg["id"]))
            db.bump_send_count(conn, today, box["email"], "first" if msg["step"] == 0 else "followup")
            from .scoring import get_opportunity_mode
            mode = get_opportunity_mode(lead)
            if msg["step"] == 0:
                new_sent_today += 1
                db.bump_send_count(conn, today, box["email"], f"first_{mode}")
                db.set_lead(conn, lead["id"], status="active", inbox=box["email"])
                personalize.schedule_followups(conn, lead["id"], now)
            elif msg["step"] >= len(config.settings()["sequence"]["followup_days"]):
                db.set_lead(conn, lead["id"], status="finished")

            outreach_logging.log_event(
                "outreach.send",
                "sent",
                lead_id=lead["id"],
                message_id=msg["id"],
                mode=mode,
                duration_ms=dur_ms,
                inbox=box["email"],
                recipient=lead["email"],
                step=msg["step"]
            )

            # Record outcome tracking
            try:
                score_val = lead["score_total"] if "score_total" in lead.keys() and lead["score_total"] else lead["score"] or 0
                db.record_message_outcome(
                    conn,
                    message_id=msg["id"],
                    lead_id=lead["id"],
                    lead_source=lead["source"] or "",
                    source_type=lead["email_source"] or "",
                    mode=mode,
                    opportunity_score=int(score_val),
                    company_stage=lead["segment"] or "",
                    contact_role=lead["title"] or "",
                    email_confidence=int(lead["confidence"] or 80) if "confidence" in lead.keys() and lead["confidence"] else 80,
                    evidence_confidence=0.9 if lead["verified_evidence"] and lead["verified_evidence"] != "[]" else 0.5,
                    personalization_confidence=float(msg["confidence"] or 0.8),
                    message_angle=lead["angle"] or "",
                    cta_type=seg.get("cta", ""),
                    subject_variant=msg["subject"] or "",
                    sequence_variant=f"step_{msg['step']}",
                    sent_at=now.isoformat(),
                    outcome="delivered",
                )
            except Exception:
                pass
            conn.commit()
            sent += 1
    if not dry_run:
        with db.connect() as conn:
            db.set_state(conn, "engine:heartbeat", now.isoformat(timespec="seconds"))

    if sent > 0:
        try:
            with db.connect() as conn:
                alloc_counts = db.get_daily_allocation_counts(conn, today)
                alloc_settings = config.allocation_settings()
                from . import alerts
                alerts.notify_send_summary(
                    sent_count=sent,
                    total_today=alloc_counts.get("total", 0),
                    ceiling=alloc_settings.get("daily_send_limit", 28),
                    freelance_count=alloc_counts.get("freelance", 0),
                    internship_count=alloc_counts.get("internship", 0),
                    followup_count=alloc_counts.get("followup", 0),
                    dry_run=dry_run,
                )
        except Exception as e:
            print(f"  telegram send notification error: {e}")

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
