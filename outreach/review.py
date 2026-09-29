"""The one manual step: read each drafted sequence and approve, edit, regenerate or drop it.

Budget ~15-20 minutes a day for 38 leads. Anything you approve is sent automatically inside the
lead's business hours, and its follow-ups go out on schedule unless the lead replies.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap

from . import db, personalize

SEP = "\n----- {label} -----\n"


def _show(lead, msgs) -> None:
    sig = db.signals(lead)
    on = [k for k, v in sig.items() if v is True and k != "reachable"]
    print("\n" + "=" * 78)
    print(f"{lead['company'] or lead['domain']}  |  {lead['first_name']} {lead['last_name']} "
          f"<{lead['email']}> [{lead['email_status']}]  |  {lead['segment']}  score={lead['score']}")
    print(f"site: {lead['website']}   signals: {', '.join(on) or '-'}")
    brief = db.research(lead)
    if brief:
        print(f"brief: {brief.get('company_summary', '')}")
        print(f"hook:  {brief.get('best_hook', '')}   fit={lead['fit']} ({brief.get('fit_reason', '')[:80]})")
    if msgs and msgs[0]["review_note"]:
        print(f"note:  {msgs[0]['review_note']}   confidence={msgs[0]['confidence']:.2f}")
    for m in msgs:
        label = "EMAIL 1" if m["step"] == 0 else f"FOLLOW-UP {m['step']} (day {m['due_at']})"
        print(SEP.format(label=label).rstrip())
        if m["step"] == 0:
            print(f"Subject: {m['subject']}\n")
        print(textwrap.fill(m["body"], 78, replace_whitespace=False))
    if lead["linkedin_note"]:
        print(SEP.format(label="LINKEDIN (send by hand)").rstrip())
        print(f"note: {lead['linkedin_note']}\ndm:   {lead['linkedin_dm']}")


def _edit(msgs) -> list[tuple[int, str, str]] | None:
    parts = []
    for m in msgs:
        head = f"SUBJECT: {m['subject']}\n" if m["step"] == 0 else ""
        parts.append(SEP.format(label=f"STEP {m['step']}") + head + m["body"])
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write("".join(parts).lstrip())
        path = f.name
    subprocess.call([os.environ.get("EDITOR", "nano"), path])
    text = open(path).read()
    os.unlink(path)
    chunks = text.split("----- STEP ")[1:]
    if len(chunks) != len(msgs):
        print("Could not parse the edited file (keep the '----- STEP n -----' lines). Nothing saved.")
        return None
    out = []
    for chunk, m in zip(chunks, msgs):
        body = chunk.split("-----\n", 1)[1].strip()
        subject = m["subject"]
        if body.startswith("SUBJECT:"):
            first, body = body.split("\n", 1)
            subject = first.removeprefix("SUBJECT:").strip()
            body = body.strip()
        out.append((m["id"], subject, body))
    return out


def approve_lead(conn, lead_id: int) -> None:
    conn.execute("UPDATE messages SET status='approved' WHERE lead_id=? AND status='draft'", (lead_id,))
    db.set_lead(conn, lead_id, status="approved")


def interactive() -> None:
    with db.connect() as conn:
        leads = conn.execute(
            "SELECT l.* FROM leads l JOIN messages m ON m.lead_id=l.id AND m.step=0 "
            "WHERE l.status='drafted' ORDER BY m.confidence DESC").fetchall()
    print(f"{len(leads)} drafted sequences to review. "
          "[a]pprove  [e]dit  [r]egenerate  [s]kip  [x] reject lead  [q]uit")
    for i, lead in enumerate(leads, 1):
        while True:
            with db.connect() as conn:
                msgs = conn.execute("SELECT * FROM messages WHERE lead_id=? ORDER BY step", (lead["id"],)).fetchall()
            _show(lead, msgs)
            choice = input(f"\n[{i}/{len(leads)}] a/e/r/s/x/q > ").strip().lower()
            if choice == "a":
                with db.connect() as conn:
                    approve_lead(conn, lead["id"])
                break
            if choice == "e":
                edited = _edit(msgs)
                if edited:
                    with db.connect() as conn:
                        for mid, subject, body in edited:
                            conn.execute("UPDATE messages SET subject=?, body=? WHERE id=?", (subject, body, mid))
                        conn.execute("UPDATE messages SET subject='Re: ' || (SELECT subject FROM messages "
                                     "WHERE lead_id=? AND step=0) WHERE lead_id=? AND step>0",
                                     (lead["id"], lead["id"]))
                continue  # show again so you can approve
            if choice == "r":
                seq = personalize.generate(lead)
                if seq:
                    personalize.save(lead["id"], seq)
                continue
            if choice == "x":
                with db.connect() as conn:
                    conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=?", (lead["id"],))
                    db.set_lead(conn, lead["id"], status="rejected")
                break
            if choice == "s":
                break
            if choice == "q":
                return


def bulk_approve(min_confidence: float) -> int:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT l.id FROM leads l JOIN messages m ON m.lead_id=l.id AND m.step=0 "
            "WHERE l.status='drafted' AND m.confidence >= ? AND l.email_status IN ('valid','risky')",
            (min_confidence,)).fetchall()
        for r in rows:
            approve_lead(conn, r["id"])
    return len(rows)


def linkedin_tasks(path) -> int:
    """Today's hand-sent LinkedIn touches: newly approved/active leads with a drafted note."""
    from urllib.parse import quote
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE linkedin_note != '' AND status IN ('approved','active') "
            "AND date(updated_at) >= date('now','-1 day') ORDER BY fit DESC LIMIT 15").fetchall()
    lines = ["# LinkedIn touches for today (by hand, max ~10/day)\n",
             "Free accounts get only a few custom notes a month: if the note box is locked, connect "
             "without a note and send the DM after they accept.\n"]
    for r in rows:
        who = f"{r['first_name']} {r['last_name']}".strip() or "founder"
        q = quote(f"{who if r['first_name'] else 'founder'} {r['company']}")
        lines += [f"## {r['company']} ({who})",
                  f"- Find: https://www.linkedin.com/search/results/people/?keywords={q}",
                  f"- Note: {r['linkedin_note']}", f"- DM after accept: {r['linkedin_dm']}", ""]
    path.write_text("\n".join(lines))
    return len(rows)
