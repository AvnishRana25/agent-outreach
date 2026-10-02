"""Writes each researched lead's sequence with Gemini: email 1, follow-ups and LinkedIn texts.

One call per lead, grounded in the research brief, so a single morning review covers the whole
sequence. The system prompt lives in prompts/draft_system.md; edit it there.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from . import config, db, llm
from .sources import rejects_ai_application


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


BANNED_FREELANCE_PHRASES = [
    re.compile(r"\b(?:resume|cv|curriculum vitae|years of experience|hire me)\b", re.I),
]

BANNED_INTERNSHIP_PHRASES = [
    re.compile(r"\b(?:passionate about|would love the opportunity|admire your company|eager to learn|aspiring)\b", re.I),
]


def pick_angle(conn, segment: str) -> dict | None:
    """A/B test: give each lead the segment's angle that has been used least so far."""
    angles = config.segment(segment).get("angles") or []
    if not angles:
        return None
    used = {r["angle"]: r["n"] for r in conn.execute(
        "SELECT angle, COUNT(*) n FROM leads WHERE segment=? AND angle != '' GROUP BY angle", (segment,))}
    return min(angles, key=lambda a: (used.get(a["id"], 0), angles.index(a)))


def system_prompt(segment: str, angle: dict | None = None, mode: str | None = None) -> str:
    from .scoring import get_opportunity_mode, MODE_INTERNSHIP, MODE_FREELANCE
    p = config.profile()
    seg = config.segment(segment)
    days = _followup_days() + [None] * 3
    if not mode:
        mode = MODE_INTERNSHIP if seg.get("signature") == "internship" or "intern" in segment else MODE_FREELANCE

    playbook = (f"Segment: {segment}\nAudience: {seg['audience'].strip()}\nTheir pain: {seg['pain'].strip()}\n"
                f"Offer: {seg['offer'].strip()}\nCTA: {seg['cta'].strip()}\nTone: {seg.get('tone', '')}")
    if seg.get("price"):
        playbook += (f"\nPrice and terms: {seg['price']}. Mention the fixed price or the trial terms once, in the "
                     "offer sentence of email 1 or in follow-up 1, so it reads as a small, safe first step.")
    if angle:
        playbook += (f"\nAngle for this email (an A/B test, so stick to it): {angle['focus']} "
                     "Build the hook and follow-up 1 around this angle.")
    base_prompt = llm.load_prompt("draft_system.md").format(
        name=p["name"],
        identity="\n".join(f"- {x}" for x in p["identity"]),
        proof="\n".join(f"- [{x['id']}] {x['text']}" for x in p["proof_points"]),
        delivery_promise=p.get("delivery_promise", ""),
        segment_playbook=playbook,
        market_style=seg.get("market_style", "International English, concise and professional."),
        d1=days[0] or "-", d2=days[1] or "-", d3=days[2] or "-",
        **_role_words(seg),
    )

    if mode == MODE_INTERNSHIP:
        mode_section = (
            "\n# Mode Guidelines: INTERNSHIP\n"
            "- Structure: Specific company/technical relevance -> evidence of ability -> concrete contribution -> low-friction CTA.\n"
            "- Focus on technical specifics: reference stack, architecture, or public engineering problem.\n"
            "- Strictly avoid generic enthusiastic phrases ('passionate about', 'would love the opportunity', 'admire your company', 'eager to learn').\n"
            "- Low-friction CTA: ask if a short technical scope or work sample would help.\n"
        )
    else:
        mode_section = (
            "\n# Mode Guidelines: FREELANCE\n"
            "- Structure: Problem -> evidence that I understand it -> concrete contribution -> outcome -> low-friction CTA.\n"
            "- Focus on commercial/operational pain and defined deliverables.\n"
            "- Strictly avoid CV-style pitching (do NOT mention resume, CV, curriculum vitae, years of experience, or 'hire me').\n"
            "- Low-friction CTA: e.g. 'I can outline how I\\'d implement this if useful.' or 'I can send a short technical proposal.'\n"
        )

    return base_prompt + mode_section


def _role_words(seg: dict) -> dict:
    """Freelance emails must not sound junior. Internship emails have to say what he's asking for,
    so there the word is allowed and he says it plainly, once."""
    if seg.get("signature") == "internship":
        return {"extra_banned": "", "role_rule": (
            "- This email asks for an internship or contract role. Say so plainly, once, in the offer sentence "
            "(e.g. 'a paid trial project, then an internship or contract role'), led by what he would build for "
            "them. Never apologise for it or sound junior.")}
    return {"extra_banned": ', "intern", "internship"', "role_rule": ""}


def lead_prompt(row) -> str:
    n = len(_followup_days())
    lead = {k: row[k] for k in ("first_name", "last_name", "title", "company", "website", "city", "country")}
    lead["email_is_generic_inbox"] = row["email_status"] == "risky"
    return (f"Write email 1, exactly {n} follow-up(s), a LinkedIn note and a LinkedIn message.\n\n"
            f"RESEARCH BRIEF\n{row['research']}\n\nLEAD\n{json.dumps(lead, indent=2)}")


def generate(row, angle: dict | None = None) -> Sequence | None:
    from . import evidence
    from .scoring import get_opportunity_mode, MODE_INTERNSHIP, MODE_FREELANCE
    mode = get_opportunity_mode(row)
    if angle is None and row["angle"]:
        angle = next((a for a in config.segment(row["segment"]).get("angles") or [] if a["id"] == row["angle"]), None)
    seq = llm.generate(system_prompt(row["segment"], angle, mode=mode), lead_prompt(row), Sequence, kind="draft", temperature=0.8)
    if seq and len(seq.followups) >= len(_followup_days()):
        seq.linkedin_note = seq.linkedin_note[:200]
        seq.linkedin_dm = seq.linkedin_dm[:450]
        ev_records = evidence.extract_evidence_from_lead(row)
        n_facts = evidence.count_verified_facts(ev_records)
        is_grounded, ground_err = evidence.validate_personalization_against_evidence(seq.body, seq.subject, ev_records)
        if not is_grounded:
            seq.confidence = min(seq.confidence, 0.5)
            seq.review_note = (seq.review_note + f" | Grounding issue: {ground_err}").strip(" |")
        elif n_facts < 1:
            seq.confidence = min(seq.confidence, 0.6)
            seq.review_note = (seq.review_note + " | Low evidence: fewer than 1 verified fact").strip(" |")

        # Check mode-specific banned phrases
        banned_pats = BANNED_INTERNSHIP_PHRASES if mode == MODE_INTERNSHIP else BANNED_FREELANCE_PHRASES
        found_banned = []
        full_text = f"{seq.subject} {seq.body} " + " ".join(f.body for f in seq.followups)
        for pat in banned_pats:
            m = pat.search(full_text)
            if m:
                found_banned.append(m.group(0))
        if found_banned:
            seq.confidence = min(seq.confidence, 0.5)
            seq.review_note = (seq.review_note + f" | Banned {mode} phrasing: {', '.join(found_banned)}").strip(" |")

        return seq
    return None


def mock(row) -> Sequence:
    name = row["first_name"] or f"{row['company']} team"
    return Sequence(
        subject=f"{(row['company'] or 'your').lower()} leads",
        body=f"Hi {name},\n\n[MOCK DRAFT for {row['company']}] Run without --mock for a real draft.",
        followups=[FollowUp(body=f"[MOCK follow-up {i + 1}]") for i in range(len(_followup_days()))],
        linkedin_note="[mock note]", linkedin_dm="[mock dm]", confidence=0.0, review_note="mock draft")


def _auto_sequence(row) -> Sequence | None:
    """Fixed copy for recent leads with independently checked company identity."""
    sending = config.settings()["sending"]
    segment = row["segment"]
    if (int(sending.get("auto_approve_daily_cap", 0)) <= 0
            or segment not in sending.get("auto_approve_segments", [])
            or (sending.get("allowed_segments") is not None and segment not in sending["allowed_segments"])
            or row["email_status"] != "valid" or row["email_source"] not in ("website", "post")
        or row["fit"] is None or row["fit"] < config.settings().get("targeting", {}).get("min_fit", 6)
            or not all(row[k] for k in ("email", "company", "website", "source_text", "site_text", "created_at"))
            or len(row["company"]) > 60 or re.search(r"[\r\n]", row["company"])
            or any(rejects_ai_application(row[k] or "") for k in ("source_text", "site_text", "notes", "research"))):
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])
        host = (urlparse(row["website"]).hostname or "").removeprefix("www.").lower()
    except (TypeError, ValueError):
        return None
    if not timedelta(0) <= age <= timedelta(days=30) or row["email"].split("@")[-1].lower() != host:
        return None

    if segment == "uk_agencies":
        number = re.search(r"company no\.\s*([A-Z0-9]+)", row["source_text"], re.I)
        if (row["source"] != "companies_house" or not number
                or number.group(1).lower() not in row["site_text"].lower()
                or "agency" not in row["site_text"].lower()):
            return None
        subject = f"A small integration for {row['company']}"
        body = (f"Hi {row['company']} team,\n\nI build CRM integrations and WhatsApp follow-up workflows for small agencies. "
                "For a real-estate brokerage, I built a production lead-handling system that assigns enquiries and sets callback reminders.\n\n"
                "I could take one scoped client integration as a GBP 200 fixed-price trial, with a written scope before work. "
                "Would a one-page outline be useful?")
    elif segment == "india_startups_intern":
        source = row["source_text"]
        posted = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", source)
        try:
            if posted:
                post_date = datetime.fromisoformat(posted.group())
            else:
                month = re.search(r"\b[A-Z][a-z]+ 20\d{2}\b", source)
                post_date = datetime.strptime(month.group(), "%B %Y")
            post_age = datetime.now(timezone.utc) - post_date.replace(tzinfo=timezone.utc)
        except (AttributeError, ValueError):
            return None
        source_type = row["source"] or ""
        if (not (source_type.startswith("jobs_") or source_type == "hn_hiring")
                or not timedelta(0) <= post_age <= timedelta(days=30)
                or not re.search(r"\b(python|automation|ai|llm|backend|full.stack|software|engineering|engineer)\b", source, re.I)):
            return None
        subject = f"Paid trial for {row['company']}"
        body = (f"Hi {row['company']} team,\n\nI saw your recent engineering hiring post. "
                "I build production WhatsApp and CRM workflows and have also worked on data and product engineering.\n\n"
                "I could take one small paid, one-week task related to your opening, share a working demo, "
                "and discuss an internship or contract role if the work is useful. Would a short scope and work sample help?")
    else:
        return None
    followups = [
        FollowUp(body=f"Hi {row['company']} team,\n\nI can send a concise scope for one trial task and show the relevant work I have already built. Would that be useful?"),
        FollowUp(body=f"Hi {row['company']} team,\n\nIf there is no suitable task right now, I will close this thread. Should I send the short scope?")
    ]
    return Sequence(subject=subject, body=body, followups=followups,
                    linkedin_note="", linkedin_dm="", confidence=0.9, review_note="Fixed template from verified public source")


def pick_leads(conn, limit: int, segment: str | None = None) -> list:
    """Fill each segment's daily quota; top up from other segments; cap India's share."""
    segs = config.settings()["segments"]
    if segment:
        segs = {k: v for k, v in segs.items() if k == segment}
    india_max = config.settings().get("targeting", {}).get("india_share_max", 0.25)
    total_quota = sum(s.get("daily_new", 0) for s in segs.values()) or 1

    def fetch(seg, n, exclude):
        marks = ",".join("?" * len(exclude)) or "-1"
        return conn.execute(
            f"SELECT * FROM leads WHERE status='researched' AND fit>=? AND email_status='valid' "
            f"AND segment=? AND id NOT IN ({marks}) "
            "ORDER BY fit DESC, score DESC, id LIMIT ?",
            (config.settings().get("targeting", {}).get("min_fit", 6), seg, *exclude, n)).fetchall()

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
        if segs.get(r["segment"], {}).get("market") == "india":
            if not segment and india >= india_cap:
                continue
            india += 1
        out.append(r)
    return out[:limit]


def run(limit: int, use_mock: bool = False, segment: str | None = None) -> int:
    with db.connect() as conn:
        rows = pick_leads(conn, limit, segment=segment)
    made = 0
    for row in rows:
        with db.connect() as conn:
            angle = pick_angle(conn, row["segment"]) if not row["angle"] else None
            if angle:
                db.set_lead(conn, row["id"], angle=angle["id"])
        try:
            auto_seq = None if use_mock else _auto_sequence(row)
            seq = mock(row) if use_mock else auto_seq or generate(row, angle)
        except llm.QuotaExhausted as e:
            print(f"  stopped drafting: {e}")
            break
        if seq is None:
            print(f"  ! {row['company']}: no usable draft; will retry next run")
            continue
        save(row["id"], seq, auto_approve=auto_seq is not None)
        made += 1
        print(f"  drafted {row['company'][:30]:<30} {row['email'] or '':<34} conf={seq.confidence:.2f} '{seq.subject}'")
    return made


def save(lead_id: int, seq: Sequence, auto_approve: bool = False) -> None:
    days = _followup_days()
    with db.connect() as conn:
        conn.execute("DELETE FROM messages WHERE lead_id=? AND status IN ('draft','approved')", (lead_id,))
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, confidence, review_note, idempotency_key) VALUES (?,?,?,?,?,?,?)",
                     (lead_id, 0, seq.subject, seq.body, seq.confidence, seq.review_note, f"lead:{lead_id}:step:0"))
        for i, fu in enumerate(seq.followups[:len(days)], start=1):
            # due_at holds the day offset until email 1 is sent, then a timestamp.
            conn.execute("INSERT INTO messages (lead_id, step, subject, body, confidence, due_at, idempotency_key) VALUES (?,?,?,?,?,?,?)",
                         (lead_id, i, "Re: " + seq.subject, fu.body, seq.confidence, str(days[i - 1]), f"lead:{lead_id}:step:{i}"))
        db.set_lead(conn, lead_id, status="drafted", linkedin_note=seq.linkedin_note, linkedin_dm=seq.linkedin_dm)
        if auto_approve:
            lead = conn.execute("SELECT email, company, email_status, email_source, fit FROM leads WHERE id=?", (lead_id,)).fetchone()
            day = datetime.now(timezone.utc).date().isoformat()
            key = f"auto_approved:{day}"
            used = int(db.get_state(conn, key, "0"))
            duplicate = conn.execute("SELECT 1 FROM leads WHERE id!=? AND lower(company)=lower(?) "
                                     "AND status IN ('approved','active')", (lead_id, lead["company"])).fetchone()
            if (used < int(config.settings()["sending"]["auto_approve_daily_cap"])
                    and lead["email_status"] == "valid" and lead["email_source"] in ("website", "post")
            and lead["fit"] is not None and lead["fit"] >= config.settings().get("targeting", {}).get("min_fit", 6)
                    and not db.suppressed(conn, lead["email"]) and not duplicate
                    and not config.placeholders()
                    and seq.confidence >= 0.85
                    and all((text or "").strip() and not config.PLACEHOLDER.search(text)
                            for text in [seq.subject, seq.body, *(f.body for f in seq.followups[:len(days)])])):
                from .review import approve_lead
                approve_lead(conn, lead_id, by="auto")
                db.set_state(conn, key, used + 1)


def schedule_followups(conn, lead_id: int, sent_at: datetime) -> None:
    for m in conn.execute("SELECT id, due_at FROM messages WHERE lead_id=? AND step>0", (lead_id,)):
        due = sent_at + timedelta(days=int(m["due_at"]))
        conn.execute("UPDATE messages SET due_at=? WHERE id=?", (due.astimezone(timezone.utc).isoformat(), m["id"]))
