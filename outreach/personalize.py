"""Writes each lead's 4-email sequence with Claude, grounded in the enrichment signals.

One call per lead returns the first email plus 3 follow-ups, so a single morning review covers
the whole sequence and nothing needs your attention again until someone replies.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import anthropic
from pydantic import BaseModel, Field

from . import config, db


class FollowUp(BaseModel):
    body: str = Field(description="Plain-text follow-up body, no greeting line repeated from email 1, no signature")


class Sequence(BaseModel):
    hook: str = Field(description="The one specific, verifiable observation about this lead the email is built on")
    subject: str = Field(description="2-6 words, lowercase except names, no clickbait, no emojis")
    body: str = Field(description="First email body: greeting line + 50-110 words, no signature")
    followups: list[FollowUp] = Field(description="Exactly 3 follow-ups, in order")
    confidence: float = Field(description="0-1: how sure you are the hook is accurate and the fit is real")
    review_note: str = Field(description="Anything the human reviewer should check; empty if nothing")


RULES = """\
You write cold emails for {name}. They are sent one at a time from a real inbox, and a human
reads every draft before it goes out.

WHO {name_upper} IS (use only these facts, never invent others):
{identity}

PROOF POINTS (pick the ONE most relevant to the lead; quote numbers exactly as written):
{proof}

HARD RULES
- Open with the lead's first name ("Hi Rohit,"). If no first name, use "Hi {{company}} team,".
- Sentence 1-2: the specific observation (hook) from THEIR website/signals, and why it matters
  to them in money, time or lost leads. No compliments about their "amazing website".
- Then one proof point that mirrors their situation. Then the offer in one sentence.
- End with ONE low-friction question as the CTA (the segment playbook says which).
- 50-110 words for email 1. Follow-ups 25-70 words. Plain text only: no links, no bullet
  lists, no bold, no emojis, no attachments mentioned. The signature is added separately.
- Never say: "I hope this email finds you well", "I came across", "synergy", "leverage",
  "quick call", "just following up", "touching base", "intern", "student", "fresher",
  "beginner", "learning". Never claim years of experience or clients that aren't in the facts.
- Never mention the employer under NDA beyond the job title given.
- Indian English is fine; write like a sharp operator talking to a busy owner, not a marketer.
- Follow-up 1 (day {d1}): a new angle or a concrete idea for them, not a reminder.
- Follow-up 2 (day {d2}): a tiny, specific win they could implement even without {name}
  (gives value first), plus the offer again in one line.
- Follow-up 3 (day {d3}): short, polite close ("Should I close the loop on this?").
- If the signals are thin or contradictory, write a safer generic-but-relevant hook, lower
  confidence below 0.6 and say why in review_note. Never guess facts about the business.

SEGMENT PLAYBOOKS
{segments}
"""


def _system_prompt() -> str:
    p = config.profile()
    s = config.settings()
    days = s["sequence"]["followup_days"]
    segs = "\n\n".join(
        f"[{key}]\nAudience: {seg['audience']}\nPain: {seg['pain']}\nOffer: {seg['offer']}\n"
        f"CTA: {seg['cta']}\nTone: {seg.get('tone', 'direct, specific, respectful')}"
        for key, seg in s["segments"].items()
    )
    return RULES.format(
        name=p["name"], name_upper=p["name"].upper(),
        identity="\n".join(f"- {x}" for x in p["identity"]),
        proof="\n".join(f"- [{x['id']}] {x['text']}" for x in p["proof_points"]),
        d1=days[0], d2=days[1], d3=days[2], segments=segs,
    )


def _lead_prompt(row) -> str:
    sig = db.signals(row)
    lead = {
        "segment": row["segment"],
        "first_name": row["first_name"], "last_name": row["last_name"], "title": row["title"],
        "company": row["company"], "website": row["website"], "country": row["country"],
        "city": row["city"], "notes_from_research": row["notes"],
        "email_is_generic_inbox": row["email_status"] == "risky",
    }
    return (
        "Write the sequence for this lead.\n\n"
        f"LEAD\n{json.dumps(lead, indent=2)}\n\n"
        f"WEBSITE SIGNALS (auto-detected, true = found in the HTML)\n{json.dumps(sig, indent=2)}\n\n"
        f"WEBSITE TEXT (truncated)\n{row['site_text'] or '(no website text)'}"
    )


def generate(row, client: anthropic.Anthropic) -> Sequence | None:
    response = client.messages.parse(
        model=config.model(),
        max_tokens=16000,
        output_config={"effort": "medium"},
        # The system prompt is identical for every lead, so it is cached after the first call.
        system=[{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _lead_prompt(row)}],
        output_format=Sequence,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return None
    return response.parsed_output


def mock(row) -> Sequence:
    """Offline template used by --mock, for testing the pipeline without an API key."""
    name = row["first_name"] or f"{row['company']} team"
    return Sequence(
        hook=f"{row['company']} website", subject=f"{(row['company'] or 'your').lower()} leads",
        body=f"Hi {name},\n\n[MOCK DRAFT for {row['company']}] Replace by running without --mock.",
        followups=[FollowUp(body=f"[MOCK follow-up {i}]") for i in (1, 2, 3)],
        confidence=0.0, review_note="mock draft",
    )


def pick_leads(conn, limit: int) -> list:
    """Fill today's quota per segment, best-scored leads first."""
    quotas = {k: v.get("daily_new", 0) for k, v in config.settings()["segments"].items()}
    total = sum(quotas.values()) or 1
    chosen = []
    for seg, quota in quotas.items():
        n = round(limit * quota / total)
        chosen += conn.execute(
            "SELECT * FROM leads WHERE status='verified' AND segment=? AND email_status != 'guessed' "
            "ORDER BY score DESC, id LIMIT ?", (seg, n)).fetchall()
    return chosen[:limit]


def run(limit: int, use_mock: bool = False, allow_guessed: bool = False) -> int:
    client = None if use_mock else anthropic.Anthropic()
    with db.connect() as conn:
        rows = pick_leads(conn, limit)
        if allow_guessed and len(rows) < limit:
            rows += conn.execute(
                "SELECT * FROM leads WHERE status='verified' AND email_status='guessed' "
                "ORDER BY score DESC LIMIT ?", (limit - len(rows),)).fetchall()
    made = 0
    for row in rows:
        try:
            seq = mock(row) if use_mock else generate(row, client)
        except anthropic.APIStatusError as e:
            print(f"  ! {row['email']}: API error {e.status_code}; will retry next run")
            continue
        except anthropic.APIConnectionError:
            print("  ! network error talking to the API; stopping this run")
            break
        if seq is None or len(seq.followups) < 3:
            print(f"  ! {row['email']}: no usable draft; left for tomorrow")
            continue
        save(row["id"], seq)
        made += 1
        print(f"  drafted {row['email']:<40} conf={seq.confidence:.2f}  subj='{seq.subject}'")
    return made


def save(lead_id: int, seq: Sequence) -> None:
    days = config.settings()["sequence"]["followup_days"]
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE lead_id=? AND status IN ('draft','approved')", (lead_id,))
        conn.execute(
            "INSERT INTO messages (lead_id, step, subject, body, confidence, review_note) VALUES (?,?,?,?,?,?)",
            (lead_id, 0, seq.subject, seq.body, seq.confidence, f"hook: {seq.hook}. {seq.review_note}".strip()),
        )
        for i, fu in enumerate(seq.followups[:3], start=1):
            conn.execute(
                "INSERT INTO messages (lead_id, step, subject, body, confidence, due_at) VALUES (?,?,?,?,?,?)",
                (lead_id, i, "Re: " + seq.subject, fu.body, seq.confidence, str(days[i - 1])),
            )
        db.set_lead(conn, lead_id, status="drafted")


def schedule_followups(conn, lead_id: int, sent_at: datetime) -> None:
    """Turn the stored day offsets into timestamps once email 1 has actually gone out."""
    for m in conn.execute("SELECT id, due_at FROM messages WHERE lead_id=? AND step>0", (lead_id,)):
        due = sent_at + timedelta(days=int(m["due_at"]))
        conn.execute("UPDATE messages SET due_at=? WHERE id=?", (due.astimezone(timezone.utc).isoformat(), m["id"]))
