"""Two-way sync between the engine's local database and the hosted dashboard (Turso + Vercel).

The engine and all secrets stay on your laptop. Every run (cron, every 10 minutes):
  1. pull: read the actions you took in the dashboard (approve, edit, reject, regenerate,
     send a reply, mark done) and apply them here;
  2. push: replace the dashboard's snapshot with what needs you now (drafts to review,
     replies and community posts to answer) plus funnel numbers and pipeline health.
Only that snapshot leaves the laptop, never keys or passwords.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import (analytics, community, config, db, doctor, eligibility, engine, firecrawl, growth, importer, leadview, personalize, replies, report, review, sender,
               sources, turso)

REMOTE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS dash_items (
        kind TEXT NOT NULL, id TEXT NOT NULL, sort TEXT, data TEXT NOT NULL, PRIMARY KEY (kind, id))""",
    "CREATE TABLE IF NOT EXISTS dash_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target INTEGER NOT NULL,
        payload TEXT DEFAULT '{}', status TEXT DEFAULT 'pending', result TEXT DEFAULT '',
        created_at TEXT, applied_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS actions_status ON actions (status)",
]
MAX_TEXT = 20_000


def init_remote() -> None:
    turso.run(REMOTE_SCHEMA)


# --------------------------------------------------------------------------- pull + apply
def _approve(target: int, payload: dict) -> str:
    with db.connect() as conn:
        lead = conn.execute("SELECT status, source_text, site_text, notes, research FROM leads WHERE id=?",
                            (target,)).fetchone()
        if not lead or lead["status"] != "drafted":
            return f"skipped: lead is {lead['status'] if lead else 'missing'}"
        if any(sources.rejects_ai_application(lead[field])
               for field in ("source_text", "site_text", "notes", "research")):
            return "error: source forbids AI-generated applications; respond manually"
        own = {r["id"]: r for r in conn.execute("SELECT id, step FROM messages WHERE lead_id=?", (target,))}
        edits = payload.get("edits", [])
        for e in edits:
            mid = int(e.get("id", 0))
            if mid not in own:
                return f"error: message {mid} is not part of lead {target}"
            if not str(e.get("body", ""))[:MAX_TEXT].strip():
                return "error: email body cannot be empty"
        for e in edits:
            mid = int(e["id"])
            body = str(e["body"])[:MAX_TEXT].strip()
            conn.execute("UPDATE messages SET body=? WHERE id=?", (body, mid))
            if own[mid]["step"] == 0 and e.get("subject"):
                conn.execute("UPDATE messages SET subject=? WHERE id=?", (str(e["subject"])[:300].strip(), mid))
        conn.execute("UPDATE messages SET subject='Re: ' || (SELECT subject FROM messages WHERE lead_id=? AND "
                     "step=0) WHERE lead_id=? AND step>0", (target, target))
        if conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND TRIM(COALESCE(body,''))='' LIMIT 1",
                        (target,)).fetchone():
            raise ValueError("email body cannot be empty")
        review.approve_lead(conn, target)
    return "approved"


def _reject(target: int, payload: dict) -> str:
    with db.connect() as conn:
        conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status IN ('draft','approved')",
                     (target,))
        db.set_lead(conn, target, status="rejected")
    return "rejected"


def _regenerate(target: int, payload: dict) -> str:
    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (target,)).fetchone()
    if not lead or lead["status"] != "drafted":
        return "skipped: not a draft"
    seq = personalize.generate(lead)
    if not seq:
        return "error: Gemini returned nothing usable"
    personalize.save(target, seq)
    return "regenerated"


def _reply_send(target: int, payload: dict) -> str:
    body = str(payload.get("body", "")).strip()[:MAX_TEXT]
    if not body:
        return "error: empty reply"
    with db.connect() as conn:
        r = conn.execute("SELECT handled FROM replies WHERE id=?", (target,)).fetchone()
    if not r or r["handled"]:
        return "skipped: already handled"
    replies.deliver_reply(target, body)
    return "sent"


def _reply_done(target: int, payload: dict) -> str:
    with db.connect() as conn:
        conn.execute("UPDATE replies SET handled=1 WHERE id=?", (target,))
    return "done"


def _post_done(target: int, payload: dict) -> str:
    with db.connect() as conn:
        conn.execute("UPDATE posts SET status='done' WHERE id=?", (target,))
    return "done"


def _run(job: str):
    return lambda target, payload: engine.spawn(job)


def _pause(target: int, payload: dict) -> str:
    with db.connect() as conn:
        db.set_state(conn, "sending_paused", "1")
    return "sending paused"


def _resume(target: int, payload: dict) -> str:
    with db.connect() as conn:
        db.set_state(conn, "sending_paused", "0")
    return "sending resumed"


def _add_lead(target: int, payload: dict) -> str:
    """A company added from the dashboard (usually an Ad Library advertiser)."""
    company = str(payload.get("company", "")).strip()[:120]
    website = str(payload.get("website", "")).strip()[:300]
    email = str(payload.get("email", "")).strip().lower()[:200]
    segment = str(payload.get("segment", "")).strip()
    notes = str(payload.get("notes", "")).strip()[:1000]
    if segment not in config.settings()["segments"]:
        return f"error: unknown segment {segment!r}"
    if not company or not (website or email):
        return "error: company plus a website or email are required"
    if website and "://" not in website:
        website = "https://" + website
    source = str(payload.get("source", "")).strip()[:60] or "adlibrary"
    opp_type = str(payload.get("opportunity_type", "")).strip().lower()
    with db.connect() as conn:
        fields = dict(company=company, website=website, domain=importer.domain_of(website, email),
                      email=email or None, email_source="dashboard" if email else "", segment=segment,
                      country=str(payload.get("country", "")).strip()[:60] or config.segment(segment).get("country", ""),
                      source=source, notes=notes,
                      source_text=f"Added from dashboard ({source}): {notes}" if notes else "")
        if opp_type in ("contract", "internship"):
            fields["opportunity_type"] = opp_type
        ok = db.add_lead(conn, **fields)
    return "added; it's researched and drafted on the next run" if ok else "skipped: already known or suppressed"


def _held(target: int):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM messages WHERE id=?", (target,)).fetchone()


def _send_anyway(target: int, payload: dict) -> str:
    """You read the hold reason and decided it may go: skip the overridable checks for this email."""
    m = _held(target)
    if not m or m["status"] != "approved":
        return f"skipped: email is {m['status'] if m else 'missing'}"
    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (m["lead_id"],)).fetchone()
        if not lead:
            return "error: lead missing"
        check = dict(m)
        check["override"] = 1
        reason = sender.hold_reason(lead, check, config.settings()["sending"])
        if reason:
            return f"error: {reason}"
        conn.execute("UPDATE messages SET override=1, attempts=0, hold='' WHERE id=?", (target,))
    return "will send at the next run inside business hours"


def _mark_sent(target: int, payload: dict) -> str:
    """An uncertain send that you found in your Sent folder: record it and start its follow-ups."""
    m = _held(target)
    if not m or m["status"] != "needs_reconciliation":
        return "skipped: not waiting for a decision"
    when = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute("UPDATE messages SET status='sent', sent_at=?, error='' WHERE id=?", (when.isoformat(), target))
        db.bump_send_count(conn, when.date().isoformat(), m["inbox"] or "", "first" if m["step"] == 0 else "followup")
        if m["step"] == 0:
            db.set_lead(conn, m["lead_id"], status="active", inbox=m["inbox"])
            personalize.schedule_followups(conn, m["lead_id"], when)
    return "marked as sent; follow-ups scheduled"


def _retry_send(target: int, payload: dict) -> str:
    """An uncertain send that is NOT in your Sent folder: put it back in the queue."""
    m = _held(target)
    if not m or m["status"] != "needs_reconciliation":
        return "skipped: not waiting for a decision"
    with db.connect() as conn:
        conn.execute("UPDATE messages SET status='approved', error='', attempts=0 WHERE id=?", (target,))
    return "back in the send queue"


def _promote_prospect(target: int, payload: dict) -> str:
    """Send a person found on the Prospects desk into the engine: researched, drafted, then your review."""
    segment = str(payload.get("segment", "")).strip()
    if segment not in config.settings()["segments"]:
        return f"error: unknown segment {segment!r}"
    with db.connect() as conn:
        p = db.get_prospect(conn, target)
        if not p or not p.get("final_email"):
            return "error: no email found for this prospect yet"
        if p.get("confidence_level") not in ("verified", "high_confidence"):
            return "error: only verified or high-confidence emails can be sent to the engine"
        # Seen published on a public page = as good as an address on their site; a pattern guess is not, so
        # those wait in Review > Held until you press Send anyway.
        source = "website" if p.get("source") == "public_match" else f"prospecting:{p.get('source') or 'pattern'}"
        ok = db.add_lead(conn, first_name=p.get("first_name") or "", last_name=p.get("last_name") or "",
                         title=p.get("title") or "", company=p.get("company") or p.get("domain"),
                         website=f"https://{p['domain']}" if p.get("domain") else "", domain=p.get("domain") or "",
                         email=p["final_email"], email_status="valid", email_source=source, segment=segment,
                         country=config.segment(segment).get("country", ""), source="prospecting",
                         source_text=f"Found on the Prospects desk: {p.get('full_name') or ''}, "
                                     f"{p.get('title') or 'role unknown'} at {p.get('company') or p.get('domain')}"
                                     + (f" ({p['source_url']})" if p.get("source_url") else ""),
                         status="new")
    return "sent to the engine: researched and drafted on the next run" if ok else "skipped: already known or suppressed"


def _approve_many(target: int, payload: dict) -> str:
    """'Approve all' in Review: the same as pressing Approve on each draft (each still passes the send checks)."""
    done, skipped = 0, 0
    for lid in payload.get("ids") or []:
        out = _approve(int(lid), {})
        done, skipped = (done + 1, skipped) if out == "approved" else (done, skipped + 1)
    return f"approved {done}" + (f", skipped {skipped} (no longer drafts)" if skipped else "")


def _run_all(target: int, payload: dict) -> str:
    """Engine tab: start new leads, community boards and LinkedIn posts together (each is its own job)."""
    results, failed = [], False
    for job in ("prepare", "community", "content"):
        try:
            outcome = engine.spawn(job)
            results.append(f"{job}: {outcome}")
            failed |= outcome.startswith("error:")
        except Exception as e:
            results.append(f"{job}: {e.__class__.__name__}: {e}")
            failed = True
    summary = "; ".join(results)
    return f"error: {summary}" if failed else summary


def _find_prospects(target: int, payload: dict) -> str:
    """Email searches queued from the dashboard (one person or a CSV), run in the background job."""
    from .prospecting.models import ProspectInput
    from .prospecting.pipeline.bulk import BulkProcessor
    from .prospecting.providers import budget
    people = []
    for p in payload.get("prospects") or []:
        try:
            people.append(ProspectInput(**{k: (v or None) if k in ("domain", "title", "linkedin_url") else v
                                           for k, v in p.items()}))
        except (TypeError, ValueError):
            continue
    if not people:
        return "error: no valid prospects"
    budget.new_run()
    results = BulkProcessor(target_verified=int(payload.get("target") or 38)).process_batch(people)
    found = sum(1 for r in results if r.final_email and r.confidence_level in ("verified", "high_confidence"))
    paid = sum(1 for r in results if r.verification_provider)
    return f"searched {len(results)} of {len(people)}: {found} verified or high-confidence, {paid} used a provider credit"


def _record_outcome(target: int, payload: dict) -> str:
    outcome = str(payload.get("outcome", "")).strip().lower()
    notes = str(payload.get("notes", "")).strip()[:500]
    stage = str(payload.get("stage", ""))
    with db.connect() as conn:
        row = conn.execute("SELECT lead_id FROM messages WHERE id=?", (target,)).fetchone()
        lead_id = row[0] if row else target
        db.update_message_outcome(conn, lead_id, outcome, notes, message_id=target)
        if stage:
            db.set_lead(conn, lead_id, deal_stage=stage, deal_note=notes[:500], deal_updated=db.now())
    return f"recorded outcome: {outcome}"


def _trigger_tick(target: int, payload: dict) -> str:
    dry_run = bool(payload.get("dry_run", True))
    max_sends = int(payload.get("max_sends", 2))
    sent = sender.tick(max_sends=max_sends, dry_run=dry_run)
    return f"{'simulated' if dry_run else 'sent'} {sent} message(s)"


APPLY = {"approve": _approve, "reject": _reject, "regenerate": _regenerate,
         "reply_send": _reply_send, "reply_done": _reply_done, "post_done": _post_done,
         "run_prepare": _run("prepare"), "run_community": _run("community"),
         "pause_sending": _pause, "resume_sending": _resume, "add_lead": _add_lead,
         "run_content": _run("content"),
         "set_stage": lambda t, p: growth.set_stage(t, str(p.get("stage", "")), _num(p.get("value")),
                                                    str(p.get("note", "")), str(p.get("currency", "USD")),
                                                    str(p.get("next_action", "")), str(p.get("next_due", "")),
                                                    str(p.get("opportunity_type", ""))),
         "make_plan": lambda t, p: growth.make_plan(t), "sync": lambda t, p: "synced",
         "send_anyway": _send_anyway, "mark_sent": _mark_sent, "retry_send": _retry_send,
         "promote_prospect": _promote_prospect, "find_prospects": _find_prospects,
         "approve_many": _approve_many, "run_all": _run_all,
         "draft_lead": lambda t, p: leadview.draft_one(t),
         "record_outcome": _record_outcome, "trigger_tick": _trigger_tick}
# These wait on Gemini (up to minutes when it's busy), so the engine run hands them to the background
# "assist" job instead of doing them itself.
SLOW = ("regenerate", "make_plan", "find_prospects", "draft_lead")


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def pull_safe() -> int:
    init_remote()
    return pull(slow=False)


def pull(slow: bool | None = None) -> int:
    """Apply pending actions. slow=False: only the quick ones (and start the assist job for the rest);
    slow=True: only the Gemini ones (the assist job); None: all of them."""
    [pending] = turso.run(["SELECT id, kind, target, payload FROM actions WHERE status='pending' ORDER BY id"])
    if slow is not None:
        later = [a for a in pending if (a["kind"] in SLOW) != slow]
        pending = [a for a in pending if (a["kind"] in SLOW) == slow]
        if slow is False and later:
            print(f"  {len(later)} Gemini action(s) handed to the assist job: {engine.spawn('assist')}")
    for a in pending:
        try:
            fn = APPLY.get(a["kind"])
            result = fn(int(a["target"]), json.loads(a["payload"] or "{}")) if fn else "error: unknown action"
        except Exception as e:  # one bad action must not block the rest
            result = f"error: {e.__class__.__name__}: {e}"[:300]
        status = "error" if result.startswith("error") else "applied"
        turso.run([("UPDATE actions SET status=?, result=?, applied_at=? WHERE id=?",
                    (status, result, db.now(), a["id"]))])
        print(f"  action #{a['id']} {a['kind']} {a['target']}: {result}")
    return len(pending)


# --------------------------------------------------------------------------- snapshot + push
def _review_items(conn) -> list[dict]:
    items = []
    for lead in conn.execute("SELECT l.*, m.confidence, m.review_note FROM leads l JOIN messages m ON "
                             "m.lead_id=l.id AND m.step=0 WHERE l.status='drafted' ORDER BY m.confidence DESC "
                             "LIMIT 150"):
        sig = db.signals(lead)
        brief = db.research(lead)
        msgs = conn.execute("SELECT id, step, subject, body, due_at FROM messages WHERE lead_id=? ORDER BY step",
                            (lead["id"],)).fetchall()
        score_tot = (lead["score_total"] if "score_total" in lead.keys() and lead["score_total"] else lead["score"]) or 0
        score_band = "Priority A" if score_tot >= 85 else "Priority B" if score_tot >= 75 else "Manual Review" if score_tot >= 60 else "Reject"
        items.append({
            "id": lead["id"], "company": lead["company"] or lead["domain"], "first_name": lead["first_name"],
            "last_name": lead["last_name"], "title": lead["title"], "email": lead["email"],
            "email_status": lead["email_status"], "segment": lead["segment"], "country": lead["country"],
            "city": lead["city"], "website": lead["website"], "source": lead["source"], "fit": lead["fit"],
            "score": lead["score"], "score_total": score_tot, "score_band": score_band,
            "confidence": lead["confidence"], "review_note": lead["review_note"],
            "signals": [k for k, v in sig.items() if v is True and k != "reachable"],
            "summary": brief.get("company_summary", ""), "hook": brief.get("best_hook", ""),
            "fit_reason": brief.get("fit_reason", ""), "pains": brief.get("pains", []),
            "source_text": (lead["source_text"] or "")[:700],
            "linkedin_note": lead["linkedin_note"], "linkedin_dm": lead["linkedin_dm"],
            "messages": [dict(m) for m in msgs]})
    return items


def _held_items(conn) -> list[dict]:
    """Approved emails the sender won't send yet, and sends that may or may not have gone out."""
    rows = conn.execute(
        "SELECT m.id, m.lead_id, m.step, m.subject, m.body, m.status, m.hold, m.error, m.inbox, m.confidence, "
        "l.company, l.email, l.segment, l.email_source, l.email_status, l.fit FROM messages m "
        "JOIN leads l ON l.id=m.lead_id WHERE (m.status='approved' AND m.hold != '') "
        "OR m.status='needs_reconciliation' ORDER BY m.status DESC, m.id LIMIT 100").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["body"] = (d["body"] or "")[:1500]
        d["kind"] = "uncertain" if r["status"] == "needs_reconciliation" else "held"
        # Some holds are about the lead itself (no-AI request, bad address, segment off): no Send anyway.
        d["overridable"] = d["kind"] == "held" and not any(x in (r["hold"] or "") for x in (
                    "AI-written", "may bounce", "allowed_segments", "empty",
                    "research fit", "valid address", "public or provider-verified"))
        out.append(d)
    return out


def _reply_items(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, l.company, l.first_name, l.segment FROM replies r JOIN leads l ON l.id=r.lead_id "
        "WHERE r.handled=0 AND r.category != 'bounce' ORDER BY r.received_at LIMIT 100").fetchall()  # every reply
    return [{"id": r["id"], "received_at": r["received_at"], "from": r["from_addr"], "company": r["company"],
             "first_name": r["first_name"], "segment": r["segment"], "category": r["category"],
             "summary": r["summary"], "subject": r["subject"], "body": (r["body"] or "")[:4000],
             "suggested_reply": r["suggested_reply"], "plan": r["plan"] or "", "lead_id": r["lead_id"]} for r in rows]


def _post_items(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM posts WHERE relevant=1 AND status != 'done' "
                        "ORDER BY COALESCE(NULLIF(posted_at, ''), found_at) DESC LIMIT 40").fetchall()
    return [{"id": r["id"], "source": r["source"], "title": r["title"], "url": r["url"], "author": r["author"],
             "posted_at": r["posted_at"] or r["found_at"], "reason": r["reason"], "draft_reply": r["draft_reply"],
             "body": (r["body"] or "")[:1200]} for r in rows]


def _health(conn) -> dict:
    s = config.settings().get("sending", {})
    tz = ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))
    today = datetime.now(timezone.utc).astimezone(tz).date()
    week_ago = (today - timedelta(days=7)).isoformat()
    inboxes = []
    for box in config.inboxes():
        sent7 = conn.execute("SELECT COALESCE(SUM(count),0) FROM send_log WHERE inbox=? AND day>=?",
                             (box["email"], week_ago)).fetchone()[0]
        bounced7 = conn.execute("SELECT COUNT(*) FROM replies WHERE inbox=? AND category='bounce' AND "
                                "received_at>=?", (box["email"], week_ago)).fetchone()[0]
        inboxes.append({"email": box["email"], "sent_today": db.send_count(conn, today.isoformat(), box["email"]),
                        "cap_today": sender.daily_cap(box, today), "sent_7d": sent7, "bounced_7d": bounced7,
                        "max_bounce_rate": s.get("max_bounce_rate", 0.03)})
    status = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM leads GROUP BY status")}
    daily = [dict(r) for r in conn.execute(
        "SELECT day, SUM(CASE WHEN kind='first' THEN count ELSE 0 END) first, "
        "SUM(CASE WHEN kind!='first' THEN count ELSE 0 END) followup FROM send_log WHERE day>=? "
        "GROUP BY day ORDER BY day", ((today - timedelta(days=29)).isoformat(),))]
    first_sent_today = sum(db.send_count(conn, today.isoformat(), b["email"], "first")
                           for b in config.inboxes() if b.get("enabled", True))
    from . import analytics
    alloc_counts = db.get_daily_allocation_counts(conn, today.isoformat())
    alloc_settings = config.allocation_settings()
    util = analytics.get_slot_utilization(conn, today.isoformat())
    mode_perf = analytics.get_mode_comparison(conn, days=30)
    return {"inboxes": inboxes, "lead_status": status, "daily_sends": daily,
            "first_sent_today": first_sent_today, "first_target": int(s.get("daily_first_target", 28)),
            "daily_allocation": {
                "ceiling": alloc_settings["daily_send_limit"],
                "total_sent": alloc_counts["total"],
                "freelance": {"sent": alloc_counts["freelance"], "limit": alloc_settings["daily_freelance_new_limit"]},
                "internship": {"sent": alloc_counts["internship"], "limit": alloc_settings["daily_internship_new_limit"]},
                "followup": {"sent": alloc_counts["followup"], "limit": alloc_settings["daily_followup_limit"]},
                "reallocation_enabled": alloc_settings["allow_unused_quota_reallocation"],
                "blocking_reasons": util.get("blocking_reasons", {}),
            },
            "mode_performance": mode_perf,
            "firecrawl": {"used_today": firecrawl.used_today(),
                          "cap": int((config.settings().get("firecrawl") or {}).get("daily_credit_cap", 60))},
            "today": today.isoformat()}


def _tail(name: str, lines: int = 25) -> str:
    path = config.DATA_DIR / name
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 12_000))
        return "\n".join(f.read().decode(errors="replace").splitlines()[-lines:])


def _engine(conn) -> dict:
    def state_json(key):
        try:
            return json.loads(db.get_state(conn, key) or "null")
        except json.JSONDecodeError:
            return None
    from . import llm
    return {"heartbeat": db.get_state(conn, "engine:heartbeat") or None,
            "home_timezone": config.settings().get("sending", {}).get("home_timezone", "Asia/Kolkata"),
            "gemini": llm.status(),
            "digest": state_json("digest:last"), "posts": state_json("content:linkedin_posts"),
            "retry_prepare": db.get_state(conn, "retry:prepare") or None,
            "placeholders": config.placeholders(),
            "sending_paused": db.get_state(conn, "sending_paused") == "1",
            "runner": "github_actions" if os.getenv("GITHUB_ACTIONS") else "local",
            "is_production": config.is_production(),
            "jobs": {j: {**engine.job_state(conn, j), "running": engine.is_running(j), "log": _tail(f"{j}.log")}
                     for j in engine.JOBS},
            "source_health": {row[0].removeprefix("source_health:"): state_json(row[0])
                              for row in conn.execute("SELECT key FROM prospect_state WHERE key LIKE 'source_health:%' ORDER BY key")},
            "reddit_boards": [{"name": item.get("name") or item["subreddit"], "subreddit": item["subreddit"]}
                              for item in config.settings().get("community", [])
                              if item.get("type") == "reddit" and item.get("enabled", True)],
            "segments": {k: v.get("audience", "").strip()[:140] for k, v in config.settings()["segments"].items()}}


def _linkedin_items(conn) -> list[dict]:
    rows = conn.execute("SELECT id, company, first_name, last_name, linkedin_note, linkedin_dm FROM leads WHERE "
                        "linkedin_note != '' AND status IN ('approved','active') AND date(updated_at) >= "
                        "date('now','-2 day') ORDER BY fit DESC LIMIT 15").fetchall()
    return [dict(r) for r in rows]


def _prospecting_summary(conn) -> dict:
    from .prospecting import config as p_cfg
    stats = db.get_prospecting_stats(conn)
    recent = db.list_prospects(conn, limit=50)
    limits = {
        "prospeo": p_cfg.provider_monthly_limit("prospeo"),
        "hunter": p_cfg.provider_monthly_limit("hunter"),
        "skrapp": p_cfg.provider_monthly_limit("skrapp"),
    }
    from .prospecting.providers import budget
    return {
        **stats,
        "target_daily": p_cfg.daily_verified_target(),
        "provider_limits": limits,
        "budget": budget.summary(),
        "recent": recent,
    }


def _schedule_items(conn) -> dict:
    """Upcoming scheduled sends, delivery queue, market window statuses, and daily quota."""
    s = config.settings().get("sending", {})
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    windows = []
    for seg_name, seg in config.settings().get("segments", {}).items():
        tz_str = seg.get("timezone", "Asia/Kolkata")
        try:
            tz = ZoneInfo(tz_str)
            local_time = now_utc.astimezone(tz)
            is_open = sender.in_window(seg, now_utc)
            windows.append({
                "segment": seg_name,
                "name": config.segment(seg_name).get("audience", seg_name),
                "timezone": tz_str,
                "local_time": local_time.strftime("%a %H:%M"),
                "send_days": seg.get("send_days", [1, 2, 3, 4, 5]),
                "send_windows": seg.get("send_windows", ["09:45-12:30", "14:30-17:30"]),
                "is_open": is_open,
            })
        except Exception:
            pass

    first_rows = conn.execute(
        """SELECT m.id, m.lead_id, m.step, m.subject, m.body, m.status, m.confidence, m.review_note,
                  l.company, l.domain, l.first_name, l.last_name, l.email, l.segment, l.country,
                  l.opportunity_type, l.score, l.score_total, l.email_status, l.email_source,
                  l.verified_evidence, l.fit
           FROM messages m JOIN leads l ON l.id=m.lead_id
           WHERE m.step=0 AND m.status='approved' AND l.status='approved'
           ORDER BY COALESCE(l.score_total, l.score, 0) DESC, m.id ASC
           LIMIT 50"""
    ).fetchall()

    upcoming_firsts = []
    for r in first_rows:
        d = dict(r)
        sc = (d.get("score_total") or d.get("score") or 0)
        d["score_band"] = "Priority A" if sc >= 85 else "Priority B" if sc >= 75 else "Manual Review" if sc >= 60 else "Reject"
        d["body_preview"] = (d["body"] or "")[:240]
        lead_dict = {
            "id": d["lead_id"],
            "email": d["email"],
            "first_name": d["first_name"],
            "segment": d["segment"],
            "score_total": d["score_total"] or d["score"],
            "fit": d["fit"],
            "email_status": d["email_status"],
            "email_source": d["email_source"],
        }
        elig = eligibility.evaluate_send_eligibility(lead_dict, d, conn, now=now_utc)
        d["eligible"] = elig.eligible
        d["eligibility_summary"] = elig.summary
        d["failed_gates"] = elig.failed_gates
        d["mode"] = "internship" if ("intern" in (d.get("segment") or "") or d.get("opportunity_type") in ("internship", "intern")) else "freelance"
        upcoming_firsts.append(d)

    followup_rows = conn.execute(
        """SELECT m.id, m.lead_id, m.step, m.subject, m.body, m.status, m.due_at,
                  l.company, l.email, l.first_name, l.segment, l.opportunity_type
           FROM messages m JOIN leads l ON l.id=m.lead_id
           WHERE m.step>0 AND m.status='approved' AND l.status='active'
           ORDER BY m.due_at ASC
           LIMIT 50"""
    ).fetchall()

    upcoming_followups = []
    for r in followup_rows:
        d = dict(r)
        d["body_preview"] = (d["body"] or "")[:200]
        d["is_due"] = bool(d["due_at"] and d["due_at"] <= now_iso)
        d["mode"] = "internship" if ("intern" in (d.get("segment") or "") or d.get("opportunity_type") in ("internship", "intern")) else "freelance"
        upcoming_followups.append(d)

    alloc = config.allocation_settings()
    tz = ZoneInfo(s.get("home_timezone", "Asia/Kolkata"))
    today_str = now_utc.astimezone(tz).date().isoformat()
    counts = db.get_daily_allocation_counts(conn, today_str)

    return {
        "windows": windows,
        "upcoming_firsts": upcoming_firsts,
        "upcoming_followups": upcoming_followups,
        "sending_paused": db.get_state(conn, "sending_paused") == "1",
        "kill_switch_enabled": config.is_sending_enabled(),
        "dry_run": config.is_dry_run(),
        "today": today_str,
        "allocation": {
            "daily_new_limit": alloc["daily_new_limit"],
            "total_new_sent": counts["freelance"] + counts["internship"],
            "freelance_sent": counts["freelance"],
            "freelance_limit": alloc["daily_freelance_new_limit"],
            "internship_sent": counts["internship"],
            "internship_limit": alloc["daily_internship_new_limit"],
            "followups_sent": counts["followup"],
            "remaining_new": max(0, alloc["daily_new_limit"] - (counts["freelance"] + counts["internship"])),
        }
    }


def _outcomes_summary(conn) -> dict:
    metrics_all = analytics.calculate_outcome_metrics(conn, days=30)
    metrics_fl = analytics.calculate_outcome_metrics(conn, mode="freelance", days=30)
    metrics_in = analytics.calculate_outcome_metrics(conn, mode="internship", days=30)
    mode_comp = analytics.get_mode_comparison(conn, days=30)
    sources = analytics.get_source_performance(conn, days=30)
    scores = analytics.get_priority_comparison(conn, days=30)
    ctas = analytics.get_cta_performance(conn, days=30)
    roles = analytics.get_role_performance(conn, days=30)
    recs = analytics.get_recommendations(conn)

    recent_rows = conn.execute(
        """SELECT o.id, o.message_id, o.lead_id, o.mode, o.opportunity_score, o.outcome,
                  o.sent_at, o.outcome_updated_at, o.notes, o.cta_type, o.message_angle,
                  l.company, l.email, l.first_name, l.segment, l.deal_stage
           FROM message_outcomes o JOIN leads l ON l.id = o.lead_id
           ORDER BY o.sent_at DESC LIMIT 50"""
    ).fetchall()

    recent = []
    if recent_rows:
        recent = [dict(r) for r in recent_rows]
    else:
        fallback = conn.execute(
            """SELECT m.id as message_id, m.lead_id, m.step, m.subject, m.sent_at,
                      l.company, l.email, l.first_name, l.segment, l.opportunity_type,
                      l.score, l.deal_stage, l.status as lead_status
               FROM messages m JOIN leads l ON l.id = m.lead_id
               WHERE m.status = 'sent' ORDER BY m.sent_at DESC LIMIT 50"""
        ).fetchall()
        for f in fallback:
            fd = dict(f)
            mode = "internship" if ("intern" in (fd.get("segment") or "") or fd.get("opportunity_type") in ("internship", "intern")) else "freelance"
            outcome = "won_project" if fd.get("deal_stage") == "won" else (
                "meeting" if fd.get("deal_stage") == "call_booked" else (
                    "replied" if fd.get("lead_status") == "replied" else (
                        "bounced" if fd.get("lead_status") == "bounced" else "delivered"
                    )
                )
            )
            recent.append({
                "id": fd["message_id"],
                "message_id": fd["message_id"],
                "lead_id": fd["lead_id"],
                "mode": mode,
                "opportunity_score": fd.get("score") or 0,
                "outcome": outcome,
                "sent_at": fd["sent_at"],
                "company": fd["company"],
                "email": fd["email"],
                "first_name": fd["first_name"],
                "segment": fd["segment"],
                "deal_stage": fd["deal_stage"],
                "notes": "",
            })

    return {
        "overall": metrics_all,
        "freelance": metrics_fl,
        "internship": metrics_in,
        "mode_comparison": mode_comp,
        "sources": sources,
        "scores": scores,
        "ctas": ctas,
        "roles": roles,
        "recommendations": recs,
        "recent": recent,
    }


def _doctor_summary() -> dict:
    try:
        return doctor.inspect_health()
    except Exception as e:
        return {"error": str(e)}


def snapshot() -> dict:
    with db.connect() as conn:
        db.purge_mock(conn)
        return {"review": _review_items(conn), "reply": _reply_items(conn), "post": _post_items(conn),
                "held": _held_items(conn), "lead": leadview.items(conn),
                "schedule": _schedule_items(conn), "outcomes": _outcomes_summary(conn),
                "doctor": _doctor_summary(),
                "stats": {"segments": report.funnel(conn, "segment"), "sources": report.funnel(conn, "source"),
                          "angles": report.angles(conn),
                          "contract": report.funnel(conn, "segment", opportunity_type="contract"),
                          "internship": report.funnel(conn, "segment", opportunity_type="internship")},
                "pipeline": growth.pipeline_items(conn),
                "health": _health(conn),
                "engine": {**_engine(conn), "adlib": sources.adlibrary_searches(), "linkedin": _linkedin_items(conn)},
                "prospecting": _prospecting_summary(conn)}


def push(snap: dict | None = None) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with db.connect() as conn:
            db.set_state(conn, "engine:heartbeat", now_iso)
    except Exception:
        pass
    snap = snap or snapshot()
    if "engine" in snap and isinstance(snap["engine"], dict):
        snap["engine"]["heartbeat"] = now_iso
    stmts: list = ["BEGIN", "DELETE FROM dash_items"]
    for kind in ("review", "reply", "post", "pipeline", "held", "lead"):
        for item in snap[kind]:
            sort = str(item.get("received_at") or item.get("posted_at") or item.get("confidence") or item.get("updated") or "")
            stmts.append(("INSERT INTO dash_items (kind, id, sort, data) VALUES (?,?,?,?)",
                          (kind, str(item["id"]), sort, json.dumps(item, default=str))))
    for key in ("stats", "health", "engine", "prospecting", "schedule", "outcomes", "doctor"):
        if key in snap:
            stmts.append(("INSERT OR REPLACE INTO dash_meta (key, value) VALUES (?,?)", (key, json.dumps(snap[key], default=str))))
    stmts.append("DELETE FROM dash_meta WHERE key='engine_error'")  # a run got this far, so settings are fine
    stmts.append(("INSERT OR REPLACE INTO dash_meta (key, value) VALUES (?,?)",
                  ("synced_at", json.dumps(datetime.now(timezone.utc).isoformat(timespec="seconds")))))
    # Keep the action log short.
    stmts.append(("DELETE FROM actions WHERE status!='pending' AND applied_at < ?",
                  ((date.today() - timedelta(days=14)).isoformat(),)))
    stmts.append("COMMIT")
    turso.run(stmts)
    return {k: len(snap[k]) for k in ("review", "reply", "post")}


def sync() -> None:
    init_remote()
    applied = pull()
    counts = push()
    community.write_digest(config.DATA_DIR / "opportunities_today.md")
    print(f"dashboard: applied {applied} actions, pushed {counts}")
