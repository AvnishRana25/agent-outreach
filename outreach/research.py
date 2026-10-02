"""Company research before any email is written.

Collects the source post/directory entry, the website pages scraped by `enrich`, detected
signals and recent news headlines (Google News RSS, free), then has Gemini write a brief:
what the company does, verified facts, pain hypotheses, the best hook, which proof point
fits, and a 0-10 fit score. Leads under the fit threshold are marked 'unfit' and never emailed.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests
from pydantic import BaseModel, Field

from . import config, db, llm

NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


class Fact(BaseModel):
    fact: str
    source: str = Field(description="source | website | signals | news")


class Pain(BaseModel):
    hypothesis: str
    evidence: str
    confidence: float = Field(description="0-1")


class Brief(BaseModel):
    company_summary: str = Field(description="What they sell, to whom, how they get customers; 1-2 sentences")
    facts: list[Fact] = Field(description="3-6 verified facts, each from the inputs")
    pains: list[Pain] = Field(description="1-3 pains the sender can fix, tied to evidence")
    best_hook: str = Field(description="The single most specific, verifiable observation to open with")
    proof_id: str = Field(description="id of the most relevant proof point")
    angle: str = Field(description="One line: what to offer them and why it matters to them")
    contact_first_name: str = Field(description="Decision maker's first name if the inputs name one, else empty")
    contact_role: str = Field(description="Their role if known, else the role to address (e.g. Founder)")
    fit_score: int = Field(description="0-10")
    fit_reason: str


def news(company: str, limit: int = 5) -> list[str]:
    if not company:
        return []
    try:
        r = requests.get(NEWS_RSS.format(q=quote(f'"{company}"')), timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    items = []
    for item in root.iter("item"):
        title, date = item.findtext("title") or "", item.findtext("pubDate") or ""
        items.append(f"{date[:16]}: {title}")
        if len(items) >= limit:
            break
    return items


def _system() -> str:
    p = config.profile()
    return llm.load_prompt("research_system.md").format(
        sender_summary="\n".join(f"- {x}" for x in p["identity"]),
        proof_ids="\n".join(f"- {x['id']}: {x['text']}" for x in p["proof_points"]),
    )


def _prompt(row, headlines: list[str]) -> str:
    seg = config.segment(row["segment"])
    lead = {k: row[k] for k in ("company", "first_name", "last_name", "title", "website",
                                "email", "city", "country", "notes")}
    return (
        f"SEGMENT: {row['segment']} (audience: {seg['audience'].strip()})\n\n"
        f"LEAD\n{json.dumps(lead, indent=2)}\n\n"
        f"SOURCE\n{row['source_text'] or '(none)'}\n\n"
        f"SIGNALS\n{json.dumps(db.signals(row))}\n\n"
        f"WEBSITE\n{row['site_text'] or '(no website text)'}\n\n"
        f"NEWS\n" + ("\n".join(headlines) or "(none found)")
    )


def mock_brief(row) -> Brief:
    return Brief(company_summary=f"{row['company']} (mock)", facts=[], pains=[],
                 best_hook=f"{row['company']} website", proof_id="re_pipeline", angle="mock",
                 contact_first_name=row["first_name"] or "", contact_role="Founder",
                 fit_score=8, fit_reason="mock")


def run(limit: int, use_mock: bool = False, segment: str | None = None) -> dict:
    threshold = config.settings().get("targeting", {}).get("min_fit", 6)
    counts = {"researched": 0, "unfit": 0, "failed": 0}
    order = "CASE WHEN email_status='valid' THEN 0 ELSE 1 END, score DESC, id"
    with db.connect() as conn:
        if segment:
            rows = conn.execute(f"SELECT * FROM leads WHERE status='verified' AND email_status IN ('valid','risky') "
                                f"AND segment=? ORDER BY {order} LIMIT ?",
                                (segment, limit)).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM leads WHERE status='verified' AND email_status IN ('valid','risky') "
                                f"ORDER BY {order} LIMIT ?",
                                (limit,)).fetchall()
    for row in rows:
        try:
            brief = mock_brief(row) if use_mock else llm.generate(
                _system(), _prompt(row, news(row["company"])), Brief, kind="research", temperature=0.2)
        except llm.QuotaExhausted as e:
            print(f"  stopped researching: {e}")
            break
        if brief is None:
            counts["failed"] += 1
            continue
        from . import scoring, evidence
        status = "researched" if brief.fit_score >= threshold else "unfit"
        lead_dict = {**dict(row), "research": brief.model_dump_json(), "fit": brief.fit_score}
        ev_records = evidence.extract_evidence_from_lead(lead_dict)
        opp_score = scoring.score_lead(lead_dict, brief=brief, evidence=ev_records)

        if opp_score.score_total < 60:
            status = "unfit"

        fields = {
            "research": brief.model_dump_json(),
            "fit": brief.fit_score,
            "status": status,
            "score_total": opp_score.score_total,
            "score_components": json.dumps(opp_score.components),
            "score_version": opp_score.version,
            "score_reason_summary": opp_score.reason_summary,
            "score_timestamp": opp_score.timestamp,
            "score": opp_score.score_total,
            "verified_evidence": json.dumps([e.model_dump() for e in ev_records]),
        }
        if brief.contact_first_name and not row["first_name"]:
            fields["first_name"] = brief.contact_first_name
        with db.connect() as conn:
            db.set_lead(conn, row["id"], **fields)
        counts[status] += 1
        print(f"  {status:<10} score={opp_score.score_total:<2} fit={brief.fit_score:<2} {row['company'] or row['domain']}: {brief.fit_reason[:90]}")
    return counts
