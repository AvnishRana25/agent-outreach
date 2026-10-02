"""Hybrid opportunity scoring, strict intent detection, and score bands.

Computes a normalized 0-100 OpportunityScore from multiple grounded components:
- 25% explicit intent (with strict negative intent rejection)
- 20% technical match
- 15% company / stage suitability
- 15% opportunity freshness / recency
- 10% decision-maker / contact role relevance
- 10% evidence confidence
- 5% contact confidence

Score Bands:
- 85-100: Priority A
- 75-84:  Priority B (default outbound eligibility threshold >= 75)
- 60-74:  Manual Review
- < 60:   Reject
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from pydantic import BaseModel, Field

from . import config, db
from .evidence import EvidenceRecord, VerificationStatus, extract_evidence_from_lead, filter_verified_facts
from .verification import VerificationDetail, get_lead_verification_detail


MODE_FREELANCE = "freelance"
MODE_INTERNSHIP = "internship"


def get_opportunity_mode(lead: dict | Any) -> str:
    """Determine whether an opportunity is in FREELANCE or INTERNSHIP mode."""
    lead = dict(lead) if lead is not None else {}
    mode = str(lead.get("mode") or lead.get("opportunity_type") or "").strip().lower()
    if mode in ("internship", "intern"):
        return MODE_INTERNSHIP
    if mode in ("freelance", "contract"):
        return MODE_FREELANCE
    seg = str(lead.get("segment") or "").lower()
    if "intern" in seg:
        return MODE_INTERNSHIP
    return MODE_FREELANCE


# Mode-specific scoring weights
FREELANCE_WEIGHTS = {
    "intent": 0.25,              # explicit project need & budget/commercial intent
    "technical_fit": 0.25,        # technical pain & implementation complexity
    "company_fit": 0.15,         # company suitability & stage
    "recency": 0.15,             # project urgency & recency
    "contact_relevance": 0.10,   # decision-maker access (founder/CTO/ops owner)
    "evidence_confidence": 0.05,
    "contact_confidence": 0.05,
}

INTERNSHIP_WEIGHTS = {
    "intent": 0.25,              # active hiring for intern/junior roles
    "technical_fit": 0.20,        # skill alignment (Python, ML, agents)
    "company_fit": 0.15,         # startup / engineering-team stage fit
    "recency": 0.15,             # recent engineering growth & freshness
    "contact_relevance": 0.10,   # recruiter / engineering manager relevance
    "evidence_confidence": 0.10, # demonstrable project fit
    "contact_confidence": 0.05,
}

DEFAULT_WEIGHTS = FREELANCE_WEIGHTS

# Negative intent phrases indicating the company is NOT seeking candidates/services
NEGATIVE_INTENT_PATTERNS = [
    re.compile(r"\b(?:we\s+are\s+not|we're\s+not|not\s+currently|not)\s+hiring\b", re.I),
    re.compile(r"\bno\s+(?:open\s+positions|openings|roles|jobs\s+available)\b", re.I),
    re.compile(r"\bnot\s+looking\s+for\s+(?:agency\s+help|anyone|devs|developers|engineers|interns|agencies|contractors|outsourcing)\b", re.I),
    re.compile(r"\b(?:no|strictly\s+no|unsolicited)\s+(?:agencies|recruiters|third\s+parties)\b", re.I),
    re.compile(r"\bposition\s+(?:is\s+)?closed\b|\brole\s+(?:is\s+)?closed\b|\bjob\s+(?:is\s+)?closed\b", re.I),
    re.compile(r"\bhiring\s+freeze\b", re.I),
    re.compile(r"\b\[for\s+hire\]|\bfor\s+hire\b|\bseeking\s+work\b|\blooking\s+for\s+work\b|\bavailable\s+for\s+hire\b", re.I),
]

# Explicit hiring / contract signals
FREELANCE_INTENT_PATTERNS = [
    re.compile(r"\b(?:seeking\s+freelancer|need\s+(?:a\s+)?freelancer|freelancer\s+needed|contractor\s+needed)\b", re.I),
    re.compile(r"\b(?:looking\s+for|need|hiring)\s+(?:a\s+)?(?:developer|engineer|expert|consultant|agency)\b", re.I),
    re.compile(r"\b(?:part[- ]time|contract\s+role|hourly\s+rate|fixed\s+price|project[- ]based|budget:)\b", re.I),
    re.compile(r"\[hiring\]|\bwe're\s+hiring\b|\bwe\s+are\s+hiring\b", re.I),
]

INTERNSHIP_INTENT_PATTERNS = [
    re.compile(r"\b(?:intern|internship|internships|co-op|summer\s+intern|fall\s+intern|student|trainee)\b", re.I),
    re.compile(r"\b(?:junior|entry[- ]level|associate|graduate)\s+(?:engineer|developer|software)\b", re.I),
    re.compile(r"\b(?:hiring|looking\s+for)\s+(?:an?\s+)?intern\b", re.I),
]

CORE_TECH_KEYWORDS = [
    "whatsapp", "n8n", "zapier", "make.com", "crm", "hubspot", "salesforce", "pipedrive",
    "automation", "workflow", "ai", "llm", "agent", "rag", "chatbot", "gpt", "gemini",
    "python", "react", "next.js", "node", "typescript", "fastapi", "scraping", "crawler",
    "firecrawl", "dashboard", "api", "integration"
]


class OpportunityScore(BaseModel):
    score_total: int = Field(description="Normalized 0-100 total score")
    score_band: str = Field(description="Priority A | Priority B | Manual Review | Reject")
    components: dict[str, float] = Field(default_factory=dict, description="Component scores 0-100")
    version: str = Field(default="v2", description="Scoring model version")
    mode: str = Field(default=MODE_FREELANCE, description="Opportunity mode: freelance | internship")
    reason_summary: str = Field(default="", description="Human-readable summary of score rationale")
    timestamp: str = Field(default="", description="ISO timestamp of scoring")


def detect_intent(lead: dict | Any) -> tuple[float, str]:
    """Detect explicit opportunity intent while filtering out negative intent."""
    lead = dict(lead) if lead is not None else {}
    source_text = lead.get("source_text") or ""
    notes = lead.get("notes") or ""
    title = lead.get("title") or ""
    combined = f"{source_text} {notes} {title}".strip()

    # 1. Check for negative intent (immediate rejection / zero intent score)
    for pat in NEGATIVE_INTENT_PATTERNS:
        m = pat.search(combined)
        if m:
            return 0.0, f"Negative intent detected: '{m.group(0)}' indicates no hiring/agency interest"

    mode = get_opportunity_mode(lead)
    source = lead.get("source") or ""

    if mode == MODE_INTERNSHIP:
        for pat in INTERNSHIP_INTENT_PATTERNS:
            m = pat.search(combined)
            if m:
                return 100.0, f"Explicit internship intent matched: '{m.group(0)}'"
        if any(s in source for s in ("yc", "launch_hn", "funding")):
            return 80.0, "Funded tech startup with inferred growth intent"
        return 50.0, "Tech company without explicit internship posting"
    else:
        for pat in FREELANCE_INTENT_PATTERNS:
            m = pat.search(combined)
            if m:
                return 100.0, f"Explicit contract/freelance intent matched: '{m.group(0)}'"
        if any(s in source for s in ("jobs_", "hn_freelance", "community", "forhire")):
            return 90.0, "Public project or freelance channel post"
        # B2B brokerages / agencies often have operational demand for white-label automation
        segment = lead.get("segment") or ""
        if any(seg in segment for seg in ("realestate", "agencies")):
            return 75.0, "Target SMB with operational automation opportunity"
        return 60.0, "General directory profile"


def calculate_technical_fit(lead: dict | Any) -> tuple[float, str]:
    """Calculate match with technical automation, AI, and workflow capabilities."""
    lead = dict(lead) if lead is not None else {}
    site_text = lead.get("site_text") or ""
    source_text = lead.get("source_text") or ""
    signals = lead.get("signals") or ""
    research = lead.get("research") or ""
    notes = lead.get("notes") or ""

    combined = f"{site_text} {source_text} {signals} {research} {notes}".lower()
    matches = [kw for kw in CORE_TECH_KEYWORDS if kw in combined]

    # If legacy fit score is high, honor technical match
    fit = lead.get("fit") if isinstance(lead, dict) else getattr(lead, "fit", None)

    if len(matches) >= 3 or (fit and fit >= 9):
        return 95.0, f"Strong technical match ({len(matches)} signals: {', '.join(matches[:3])})"
    elif len(matches) == 2 or (fit and fit >= 8):
        return 85.0, f"Good technical match ({len(matches)} signals: {', '.join(matches[:2])})"
    elif len(matches) == 1 or (fit and fit >= 7):
        return 75.0, f"Moderate technical match ({', '.join(matches[:1])})"
    elif fit and fit >= 6:
        return 65.0, "Basic technical alignment from brief"
    return 45.0, "Limited technical alignment detected"


def calculate_company_fit(lead: dict | Any) -> tuple[float, str]:
    """Score suitability of company size and business stage."""
    lead = dict(lead) if lead is not None else {}
    segment = lead.get("segment") or ""
    company = lead.get("company") or ""
    site_text = lead.get("site_text") or ""

    lower_site = site_text.lower()
    # Enterprise penalties (companies too large for individual contractor or cold founder outreach)
    if any(k in lower_site for k in ("fortune 500", "enterprise software conglomerate", "government agency", "10,000+ employees")):
        return 35.0, "Enterprise scale outside ideal SMB/startup profile"

    if "realestate" in segment:
        return 90.0, "Ideal real estate brokerage profile"
    elif "agencies" in segment:
        return 90.0, "Ideal boutique marketing/web/automation agency profile"
    elif "intern" in segment:
        return 88.0, "Ideal high-growth startup profile"
    return 75.0, "Suitable commercial stage"


def calculate_recency(lead: dict | Any) -> tuple[float, str]:
    """Score opportunity freshness and recency."""
    lead = dict(lead) if lead is not None else {}
    source_text = lead.get("source_text") or ""
    created_at = lead.get("created_at") or ""
    now_dt = datetime.now(timezone.utc)

    # Check for date in source text (e.g. 2026-10-01)
    m = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", source_text)
    if m:
        try:
            post_dt = datetime.fromisoformat(m.group(0)).replace(tzinfo=timezone.utc)
            age_days = (now_dt - post_dt).days
            if age_days <= 2:
                return 100.0, f"Very fresh posting ({age_days}d old)"
            elif age_days <= 7:
                return 85.0, f"Fresh posting ({age_days}d old)"
            elif age_days <= 14:
                return 70.0, f"Recent posting ({age_days}d old)"
            elif age_days <= 30:
                return 40.0, f"Aging posting ({age_days}d old)"
            else:
                return 15.0, f"Stale posting ({age_days}d old > 30d limit)"
        except ValueError:
            pass

    # Fall back to created_at
    if created_at:
        try:
            created_dt = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
            age_days = (now_dt - created_dt).days
            if age_days <= 2:
                return 95.0, f"Fresh discovery ({age_days}d old)"
            elif age_days <= 7:
                return 85.0, f"Recent discovery ({age_days}d old)"
            elif age_days <= 14:
                return 70.0, f"Moderate discovery age ({age_days}d old)"
            elif age_days <= 30:
                return 45.0, f"Older lead ({age_days}d old)"
            else:
                return 20.0, f"Stale lead record ({age_days}d old)"
        except ValueError:
            pass

    return 80.0, "Freshness inferred from active batch"


def calculate_contact_relevance(lead: dict | Any) -> tuple[float, str]:
    """Score decision-maker and contact relevance based on title or role."""
    lead = dict(lead) if lead is not None else {}
    title = lead.get("title") or ""
    email_status = lead.get("email_status") or ""
    mode = get_opportunity_mode(lead)
    t_lower = title.lower()

    if not title:
        # If no explicit title, check if it's a small brokerage/agency owner role
        if email_status == "valid":
            return 75.0, "Verified contact without specific job title"
        return 60.0, "Unspecified role"

    # Irrelevant roles
    irrelevant_keywords = ("receptionist", "office assistant", "billing", "accounts payable", "legal counsel", "compliance")
    if any(k in t_lower for k in irrelevant_keywords):
        return 20.0, f"Irrelevant contact role: {title}"

    if mode == MODE_INTERNSHIP:
        if any(k in t_lower for k in ("founder", "cto", "chief technology", "head of engineering", "vp engineering", "engineering director")):
            return 100.0, f"Target technical decision maker: {title}"
        if any(k in t_lower for k in ("engineering manager", "lead engineer", "staff engineer", "tech lead")):
            return 95.0, f"Technical hiring manager: {title}"
        if any(k in t_lower for k in ("technical recruiter", "recruiter", "talent partner", "head of talent")):
            return 90.0, f"Technical talent decision maker: {title}"
        if any(k in t_lower for k in ("hr", "human resources", "people")):
            return 65.0, f"General HR contact: {title}"
        return 40.0, f"Non-technical role for internship outreach: {title}"
    else:
        if any(k in t_lower for k in ("founder", "co-founder", "owner", "managing director", "ceo", "chief executive", "technical founder")):
            return 100.0, f"Target executive decision maker: {title}"
        if any(k in t_lower for k in ("cto", "chief technology", "head of operations", "operations director", "operations owner", "automation owner", "vp operations")):
            return 95.0, f"Operational / technical leader: {title}"
        if any(k in t_lower for k in ("agency director", "project manager", "head of digital", "lead developer", "department head")):
            return 85.0, f"Department decision maker: {title}"
        if any(k in t_lower for k in ("recruiter", "talent", "hr")):
            return 30.0, f"Recruiting contact inappropriate for freelance automation: {title}"
        return 65.0, f"Standard business contact: {title}"


def calculate_evidence_confidence(lead: dict | Any, evidence: list[EvidenceRecord]) -> tuple[float, str]:
    """Score the depth and verification status of personalization facts."""
    verified = filter_verified_facts(evidence)
    n = len(verified)
    if n >= 2:
        return 95.0, f"Strong evidence grounding ({n} verified facts)"
    elif n == 1:
        return 80.0, "Sufficient evidence grounding (1 verified fact)"
    elif len(evidence) > 0:
        return 45.0, "Only inferred evidence available"
    return 15.0, "Insufficient evidence grounding (0 verified facts)"


def calculate_contact_confidence(detail: VerificationDetail) -> tuple[float, str]:
    """Score the recipient contact deliverability confidence."""
    return float(detail.confidence), f"{detail.confidence}% email deliverability confidence ({detail.method})"


def score_lead(
    lead: dict | Any,
    brief: Any = None,
    verification: VerificationDetail | None = None,
    evidence: list[EvidenceRecord] | None = None,
    weights: dict[str, float] | None = None,
    conn: Any = None,
) -> OpportunityScore:
    """Compute the normalized 0-100 OpportunityScore and assign a score band."""
    lead = dict(lead) if lead is not None else {}
    mode = get_opportunity_mode(lead)
    if weights is None:
        weights = INTERNSHIP_WEIGHTS if mode == MODE_INTERNSHIP else FREELANCE_WEIGHTS

    # Gather evidence and verification if not provided
    if evidence is None:
        evidence = extract_evidence_from_lead(lead)
    if verification is None:
        verification = get_lead_verification_detail(lead, conn=conn)

    s_intent, r_intent = detect_intent(lead)
    s_tech, r_tech = calculate_technical_fit(lead)
    s_comp, r_comp = calculate_company_fit(lead)
    s_rec, r_rec = calculate_recency(lead)
    s_role, r_role = calculate_contact_relevance(lead)
    s_ev, r_ev = calculate_evidence_confidence(lead, evidence)
    s_cont, r_cont = calculate_contact_confidence(verification)

    components = {
        "intent": round(s_intent, 1),
        "technical_fit": round(s_tech, 1),
        "company_fit": round(s_comp, 1),
        "recency": round(s_rec, 1),
        "contact_relevance": round(s_role, 1),
        "evidence_confidence": round(s_ev, 1),
        "contact_confidence": round(s_cont, 1),
    }

    total = sum(components[k] * weights[k] for k in weights)
    # Heavy penalty for stale opportunities (> 30 days old) to block automatic sending
    if s_rec <= 20.0:
        total -= 15.0
    score_total = max(0, min(100, int(round(total))))

    # Assign band
    if score_total >= 85:
        band = "Priority A"
    elif score_total >= 75:
        band = "Priority B"
    elif score_total >= 60:
        band = "Manual Review"
    else:
        band = "Reject"

    # Summarize key drivers
    summary_parts = []
    if s_intent == 0:
        summary_parts.append(r_intent)
    else:
        summary_parts.append(f"[{mode.upper()}] {band} ({score_total}/100)")
        summary_parts.append(f"Intent={int(s_intent)} Tech={int(s_tech)} Role={int(s_role)}")
        summary_parts.append(f"VerifiedFacts={len(filter_verified_facts(evidence))} EmailConf={verification.confidence}%")

    reason_summary = " | ".join(summary_parts)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return OpportunityScore(
        score_total=score_total,
        score_band=band,
        components=components,
        version="v2",
        mode=mode,
        reason_summary=reason_summary,
        timestamp=now_iso,
    )


def save_lead_score(conn, lead_id: int, score: OpportunityScore) -> None:
    """Save computed opportunity score to the leads table."""
    conn.execute(
        """UPDATE leads SET score_total=?, score_components=?, score_version=?,
           score_reason_summary=?, score_timestamp=?, score=? WHERE id=?""",
        (
            score.score_total,
            json.dumps(score.components),
            score.version,
            score.reason_summary,
            score.timestamp,
            score.score_total,  # keep legacy score in sync
            lead_id,
        )
    )
