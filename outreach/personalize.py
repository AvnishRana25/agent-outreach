"""Writes each researched lead's sequence with Gemini: email 1, follow-ups and LinkedIn texts.

One call per lead, grounded in the research brief, so a single morning review covers the whole
sequence. The system prompt lives in prompts/draft_system.md; edit it there.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

from . import config, db, llm


class FollowUp(BaseModel):
    body: str = Field(description="Plain-text follow-up body, no signature")


class Sequence(BaseModel):
    subject: str = Field(description="2-6 words, lowercase except names")
    body: str = Field(description="Email 1: greeting line + 50-110 words, no signature")
    followups: list[FollowUp] = Field(description="Follow-ups in order, as many as requested")
    linkedin_note: str = Field(description="Connection note, max 200 characters")
    linkedin_dm: str = Field(description="Message after they accept, max 450 characters")
    confidence: float = Field(description="0-1: how sure you are every claim is accurate and the fit is real")
    review_note: str = Field(description="What the reviewer should double-check; empty if nothing")


def _followup_days() -> list[int]:
    return config.settings()["sequence"]["followup_days"]


def system_prompt(segment: str) -> str:
    p = config.profile()
    seg = config.segment(segment)
    days = _followup_days() + [None] * 3
    playbook = (f"Segment: {segment}\nAudience: {seg['audience'].strip()}\nTheir pain: {seg['pain'].strip()}\n"
                f"Offer: {seg['offer'].strip()}\nCTA: {seg['cta'].strip()}\nTone: {seg.get('tone', '')}")
    return llm.load_prompt("draft_system.md").format(
        name=p["name"],
        identity="\n".join(f"- {x}" for x in p["identity"]),
        proof="\n".join(f"- [{x['id']}] {x['text']}" for x in p["proof_points"]),
        delivery_promise=p.get("delivery_promise", ""),
        segment_playbook=playbook,
        market_style=seg.get("market_style", "International English, concise and professional."),
        d1=days[0] or "-", d2=days[1] or "-", d3=days[2] or "-",
    )


def lead_prompt(row) -> str:
    n = len(_followup_days())
    lead = {k: row[k] for k in ("first_name", "last_name", "title", "company", "website", "city", "country")}
    lead["email_is_generic_inbox"] = row["email_status"] == "risky"
    return (f"Write email 1, exactly {n} follow-up(s), a LinkedIn note and a LinkedIn message.\n\n"
            f"RESEARCH BRIEF\n{row['research']}\n\nLEAD\n{json.dumps(lead, indent=2)}")


def generate(row) -> Sequence | None:
    seq = llm.generate(system_prompt(row["segment"]), lead_prompt(row), Sequence, kind="draft", temperature=0.8)
    if seq and len(seq.followups) >= len(_followup_days()):
        seq.linkedin_note = seq.linkedin_note[:200]
        seq.linkedin_dm = seq.linkedin_dm[:450]
        return seq
    return None


def mock(row) -> Sequence:
    name = row["first_name"] or f"{row['company']} team"
    return Sequence(
        subject=f"{(row['company'] or 'your').lower()} leads",
        body=f"Hi {name},\n\n[MOCK DRAFT for {row['company']}] Run without --mock for a real draft.",
        followups=[FollowUp(body=f"[MOCK follow-up {i + 1}]") for i in range(len(_followup_days()))],
        linkedin_note="[mock note]", linkedin_dm="[mock dm]", confidence=0.0, review_note="mock draft")


def pick_leads(conn, limit: int) -> list:
    """Fill each segment's daily quota; top up from other segments; cap India's share."""
    segs = config.settings()["segments"]
    india_max = config.settings().get("targeting", {}).get("india_share_max", 0.25)
    total_quota = sum(s.get("daily_new", 0) for s in segs.values()) or 1

    def fetch(seg, n, exclude):
        marks = ",".join("?" * len(exclude)) or "-1"
        return conn.execute(
            f"SELECT * FROM leads WHERE status='researched' AND segment=? AND id NOT IN ({marks}) "
            "ORDER BY fit DESC, score DESC, id LIMIT ?", (seg, *exclude, n)).fetchall()

    chosen: list = []
    for name, seg in segs.items():
        chosen += fetch(name, round(limit * seg.get("daily_new", 0) / total_quota), [r["id"] for r in chosen])
    # Top up from non-India segments first when a segment ran dry.
    for name, seg in sorted(segs.items(), key=lambda kv: kv[1].get("market") == "india"):
        if len(chosen) >= limit:
            break
        chosen += fetch(name, limit - len(chosen), [r["id"] for r in chosen])

    india_cap = int(limit * india_max)
    out, india = [], 0
    for r in chosen:
        if segs[r["segment"]].get("market") == "india":
            if india >= india_cap:
                continue
            india += 1
        out.append(r)
    return out[:limit]


def run(limit: int, use_mock: bool = False) -> int:
    with db.connect() as conn:
        rows = pick_leads(conn, limit)
    made = 0
    for row in rows:
        try:
            seq = mock(row) if use_mock else generate(row)
        except llm.QuotaExhausted:
            print("  Gemini daily quota reached; remaining drafts wait for tomorrow")
            break
        if seq is None:
            print(f"  ! {row['company']}: no usable draft; will retry next run")
            continue
        save(row["id"], seq)
        made += 1
        print(f"  drafted {row['company'][:30]:<30} {row['email'] or '':<34} conf={seq.confidence:.2f} '{seq.subject}'")
    return made


def save(lead_id: int, seq: Sequence) -> None:
    days = _followup_days()
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE lead_id=? AND status IN ('draft','approved')", (lead_id,))
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, confidence, review_note) VALUES (?,?,?,?,?,?)",
                     (lead_id, 0, seq.subject, seq.body, seq.confidence, seq.review_note))
        for i, fu in enumerate(seq.followups[:len(days)], start=1):
            # due_at holds the day offset until email 1 is sent, then a timestamp.
            conn.execute("INSERT INTO messages (lead_id, step, subject, body, confidence, due_at) VALUES (?,?,?,?,?,?)",
                         (lead_id, i, "Re: " + seq.subject, fu.body, seq.confidence, str(days[i - 1])))
        db.set_lead(conn, lead_id, status="drafted", linkedin_note=seq.linkedin_note, linkedin_dm=seq.linkedin_dm)


def schedule_followups(conn, lead_id: int, sent_at: datetime) -> None:
    for m in conn.execute("SELECT id, due_at FROM messages WHERE lead_id=? AND step>0", (lead_id,)):
        due = sent_at + timedelta(days=int(m["due_at"]))
        conn.execute("UPDATE messages SET due_at=? WHERE id=?", (due.astimezone(timezone.utc).isoformat(), m["id"]))
