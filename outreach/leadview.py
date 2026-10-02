"""Every lead in one place, for the dashboard's Leads tab.

All leads live on your Mac in data/outreach.db, table `leads` (one row per company), with their emails in
`messages` and replies in `replies`. The dashboard shows a copy that the engine pushes every few minutes.
For each lead this works out, in plain words, where it is in the pipeline, why it stopped if it did, what
you can do next, and (for drafts) whether the safety checks would let it send.
"""
from __future__ import annotations

import json

from . import config, db, sender

# status -> (group, label)
STAGES = {
    "new": ("finding", "Found; website not read yet"),
    "enriched": ("finding", "Website read; email not checked yet"),
    "verified": ("finding", "Email checked; waiting for research"),
    "researched": ("ready", "Researched; waiting for a draft"),
    "drafted": ("review", "Draft waiting for your review"),
    "approved": ("queued", "Approved; waiting to send"),
    "active": ("sent", "Sent; follow-ups scheduled"),
    "finished": ("sent", "Sequence finished; no reply"),
    "replied": ("replied", "Replied"),
    "unfit": ("skipped", "Skipped: poor fit"),
    "invalid": ("skipped", "Skipped: no usable email"),
    "rejected": ("skipped", "Rejected"),
    "bounced": ("skipped", "Email bounced"),
    "unsubscribed": ("skipped", "Asked not to be contacted"),
}
GROUPS = ["finding", "ready", "review", "queued", "sent", "replied", "skipped"]
# Leads you can ask the engine to draft right now (it researches first when needed).
DRAFTABLE = ("new", "enriched", "verified", "researched", "unfit")


def _brief(row) -> dict:
    try:
        return json.loads(row["research"] or "{}")
    except json.JSONDecodeError:
        return {}


def why(row, msg=None) -> str:
    """Why this lead is where it is, in one line."""
    st, notes = row["status"], (row["notes"] or "")
    if st == "unfit":
        return f"Fit {row['fit']}/10: {_brief(row).get('fit_reason', 'research found no clear need')}"
    if st == "invalid":
        return "No email address could be found or checked" + (" (website unreachable)" if "unreachable" in notes else "")
    if st == "bounced":
        return "The address doesn't exist; it's on the do-not-email list"
    if st == "unsubscribed":
        return "They asked not to be contacted, or replied no"
    if st == "rejected":
        return notes.split("|")[-1].strip() if "duplicate" in notes else "You rejected the draft"
    if st in ("drafted", "approved") and msg is not None and (msg["hold"] or ""):
        return f"Held: {msg['hold']}"
    if st == "verified" and row["email_status"] == "guessed":
        return "Email is a guess from the name; the next 'Find & draft' run tries to confirm it"
    if st == "verified" and row["email_status"] == "unconfirmed":
        return "Email is a guess the email finder couldn't confirm; add the right address to draft it"
    if st == "verified" and row["email_status"] not in ("valid", "risky"):
        return f"Email is {row['email_status']}: research only runs on checked addresses"
    if st == "researched":
        return f"Fit {row['fit']}/10; drafted by the next 'Find & draft' run (drafting matches what can be sent)"
    return ""


def security_check(row, msg) -> str:
    """'' when the first email would pass the send checks, else the reason it would be held."""
    if msg is None:
        return ""
    fake = dict(msg)
    fake["approved_by"] = fake.get("approved_by") or "you"   # judged as if you approve it
    reason = sender.hold_reason(row, fake, config.settings()["sending"])
    if reason:
        return reason
    body = f"{msg['subject']}\n{msg['body']}\n{sender._signature(config.segment(row['segment']))}"
    hole = config.PLACEHOLDER.search(body)
    return f"contains template text {hole.group(0)!r}" if hole else ""


def items(conn, limit: int = 3000) -> list[dict]:
    rows = conn.execute("SELECT * FROM leads ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?", (limit,)).fetchall()
    first = {m["lead_id"]: m for m in conn.execute(
        "SELECT * FROM messages WHERE step=0 AND lead_id IN (SELECT id FROM leads ORDER BY COALESCE(updated_at, "
        "created_at) DESC LIMIT ?)", (limit,))}
    last_reply = {r["lead_id"]: r["category"] for r in conn.execute(
        "SELECT lead_id, category FROM replies r WHERE id = (SELECT MAX(id) FROM replies WHERE lead_id=r.lead_id)")}
    out = []
    for row in rows:
        group, label = STAGES.get(row["status"], ("finding", row["status"]))
        msg = first.get(row["id"])
        check = None
        if row["status"] in ("drafted", "approved") and msg is not None:
            check = security_check(row, msg) or "ok"
        score_tot = (row["score_total"] if "score_total" in row.keys() and row["score_total"] else row["score"]) or 0
        score_band = "Priority A" if score_tot >= 85 else "Priority B" if score_tot >= 75 else "Manual Review" if score_tot >= 60 else "Reject"
        out.append({
            "id": row["id"], "company": row["company"] or row["domain"] or "", "email": row["email"] or "",
            "name": " ".join(x for x in (row["first_name"], row["last_name"]) if x),
            "segment": row["segment"], "source": row["source"] or "", "status": row["status"], "group": group,
            "label": label, "why": why(row, msg), "check": check, "fit": row["fit"], "website": row["website"] or "",
            "email_status": row["email_status"], "type": row["opportunity_type"] or "",
            "score_total": score_tot, "score_band": score_band,
            "subject": msg["subject"] if msg is not None else "", "sent_at": msg["sent_at"] if msg is not None else "",
            "reply": last_reply.get(row["id"], ""), "updated": row["updated_at"] or row["created_at"] or "",
            "can_draft": row["status"] in DRAFTABLE and bool(row["email"]) and row["email_status"] in ("valid", "risky", "unchecked"),
        })
    return out


def draft_one(lead_id: int) -> str:
    """Research (if needed) and draft one lead now; the draft then waits in Review like any other."""
    from . import llm, personalize, research
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        return "error: lead not found"
    if row["status"] not in DRAFTABLE:
        return f"skipped: lead is {row['status']}"
    if not row["email"]:
        return "error: no email address for this lead"
    try:
        if not row["research"]:
            brief = llm.generate(research._system(), research._prompt(row, research.news(row["company"])),
                                 research.Brief, kind="research", temperature=0.2)
            if not brief:
                return "error: research returned nothing usable; try again"
            with db.connect() as conn:
                db.set_lead(conn, lead_id, research=brief.model_dump_json(), fit=brief.fit_score)
                row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        with db.connect() as conn:
            angle = personalize.pick_angle(conn, row["segment"]) if not row["angle"] else None
            if angle:
                db.set_lead(conn, lead_id, angle=angle["id"])
        seq = personalize.generate(row, angle)
    except llm.QuotaExhausted as e:
        return f"error: {e}"
    if not seq:
        return "error: the AI returned nothing usable; try again"
    personalize.save(lead_id, seq)
    return "drafted: it's in Review now"
