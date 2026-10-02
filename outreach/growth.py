"""What happens after a reply, and the weekly loop that improves the campaign.

  deals      track each conversation: call booked -> proposal sent -> won (with value) / lost
  plan       a one-page plan for a lead who said "yes, send the plan", written from their research
             brief and their reply, priced from the segment's fixed offer
  posts      three LinkedIn post drafts a week about AI and automation: a take on this week's AI news,
             an opinion on the AI industry, and a lesson from building (your opinions in profile.yaml)
  digest     Monday summary on Telegram: last week's numbers plus specific changes to make
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field

from . import config, db, llm, replies, report

STAGES = ("call_booked", "proposal_sent", "won", "lost")
STAGE_LABEL = {"call_booked": "Call booked", "proposal_sent": "Proposal sent", "won": "Won", "lost": "Lost"}
CURRENCIES = {"USD", "GBP", "AED", "INR"}


# --------------------------------------------------------------------------- deals
OPPORTUNITY_TYPES = {"contract", "internship"}


def set_stage(lead_id: int, stage: str, value: float | None = None, note: str = "", currency: str = "USD",
              next_action: str = "", next_due: str = "", opportunity_type: str = "") -> str:
    if stage not in STAGES and stage != "":
        return f"error: unknown stage {stage!r}"
    if currency not in CURRENCIES:
        return f"error: unknown currency {currency!r}"
    if opportunity_type and opportunity_type not in OPPORTUNITY_TYPES:
        return f"error: unknown opportunity type {opportunity_type!r}"
    try:
        if next_due:
            date.fromisoformat(next_due)
    except ValueError:
        return "error: invalid next due date"
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
            return f"error: no lead {lead_id}"
        fields = dict(deal_stage=stage, deal_value=value, deal_note=note[:500],
                      deal_currency=currency, deal_next_action=next_action[:300], deal_next_due=next_due,
                      deal_updated=db.now())
        if opportunity_type:
            fields["opportunity_type"] = opportunity_type
        db.set_lead(conn, lead_id, **fields)

        if stage:
            lead_row = conn.execute("SELECT segment, opportunity_type FROM leads WHERE id=?", (lead_id,)).fetchone()
            is_intern = (lead_row and ("intern" in str(lead_row["segment"]).lower() or lead_row["opportunity_type"] in ("internship", "intern")))
            if stage == "call_booked":
                outcome = "interview" if is_intern else "meeting"
            elif stage == "proposal_sent":
                outcome = "project_discussion"
            elif stage == "won":
                outcome = "offer" if is_intern else "won_project"
            elif stage == "lost":
                outcome = "lost_project"
            else:
                outcome = ""
            if outcome:
                db.update_message_outcome(conn, lead_id, outcome=outcome, notes=note)
    return f"stage: {STAGE_LABEL.get(stage, 'cleared')}"


def pipeline_items(conn) -> list[dict]:
    """Every lead you're in a conversation with: positive replies plus anything with a deal stage."""
    rows = conn.execute(f"""
        SELECT l.id, l.company, l.first_name, l.last_name, l.email, l.segment, l.source,
               COALESCE(NULLIF(l.opportunity_type, ''),
                        CASE WHEN l.segment LIKE '%intern%' THEN 'internship' ELSE 'contract' END) AS opportunity_type,
               l.deal_stage, l.deal_value,
               l.deal_currency, l.deal_note, l.deal_next_action, l.deal_next_due, l.deal_updated,
               (SELECT r.summary FROM replies r WHERE r.lead_id=l.id ORDER BY r.received_at DESC LIMIT 1) AS last_reply,
               (SELECT r.received_at FROM replies r WHERE r.lead_id=l.id ORDER BY r.received_at DESC LIMIT 1) AS replied_at
        FROM leads l
        WHERE l.deal_stage != '' OR EXISTS (SELECT 1 FROM replies r WHERE r.lead_id=l.id
                                            AND r.category IN {report.POSITIVE_SQL})
        ORDER BY CASE l.deal_stage WHEN 'won' THEN 3 WHEN 'lost' THEN 4 ELSE 1 END,
                 COALESCE(l.deal_updated, '') DESC LIMIT 100""").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- one-page plan
class Plan(BaseModel):
    title: str = Field(description="e.g. 'WhatsApp lead engine for Palm Homes'")
    situation: str = Field(description="2-3 sentences: where they are now, only facts from the inputs")
    steps: list[str] = Field(description="3-5 concrete build steps, one sentence each")
    timeline: str = Field(description="When each part is live, matching the offer's terms")
    price: str = Field(description="Exactly the price and terms from the offer; never invent a different number")
    needs: list[str] = Field(description="2-4 things he needs from them (access, numbers, a contact)")
    next_step: str = Field(description="One sentence: the single next step to start")


def _plan_system(segment: str) -> str:
    p, seg = config.profile(), config.segment(segment)
    return (
        f"You write a short, concrete one-page project plan that {p['name']} sends to a prospect who asked for it.\n\n"
        "WHO HE IS (only these facts)\n" + "\n".join(f"- {x}" for x in p["identity"]) +
        "\n\nPROOF POINTS (you may reference one)\n" + "\n".join(f"- {x['text']}" for x in p["proof_points"]) +
        f"\n\nTHE OFFER\n{seg['offer'].strip()}\nPrice and terms: {seg.get('price', 'fixed price agreed after scoping')}\n"
        f"Delivery promise: {p.get('delivery_promise', '')}\n\n"
        "RULES\n- Use only facts from the inputs about the prospect; if something isn't known, plan to find it out in "
        "step 1 instead of guessing.\n- Price exactly as in the offer.\n- Plain, specific language a busy owner reads "
        "in one minute. No hype, no jargon, no emojis.")


def render_plan(plan: Plan) -> str:
    lines = [plan.title, "", "Where you are now", plan.situation, "", "What I'd build"]
    lines += [f"{i}. {s}" for i, s in enumerate(plan.steps, 1)]
    lines += ["", "Timeline", plan.timeline, "", "Price", plan.price, "", "What I need from you"]
    lines += [f"- {n}" for n in plan.needs]
    lines += ["", "Next step", plan.next_step]
    return "\n".join(lines)


def make_plan(reply_id: int) -> str:
    with db.connect() as conn:
        r = conn.execute("SELECT r.*, l.segment, l.research, l.company, l.first_name, l.website, l.id AS lid "
                         "FROM replies r JOIN leads l ON l.id=r.lead_id WHERE r.id=?", (reply_id,)).fetchone()
        if not r:
            return f"error: no reply {reply_id}"
        first = conn.execute("SELECT subject, body FROM messages WHERE lead_id=? AND step=0", (r["lid"],)).fetchone()
    prompt = (f"PROSPECT: {r['company']} ({r['website']}), contact {r['first_name'] or 'unknown'}\n\n"
              f"RESEARCH BRIEF\n{r['research'] or '(none)'}\n\n"
              f"OUR FIRST EMAIL\n{(first['body'] if first else '(none)')}\n\nTHEIR REPLY\n{replies._strip_quoted(r['body'] or '')}")
    plan = llm.generate(_plan_system(r["segment"]), prompt, Plan, kind="draft", temperature=0.4)
    if not plan:
        return "error: Gemini returned nothing usable; try again"
    with db.connect() as conn:
        conn.execute("UPDATE replies SET plan=? WHERE id=?", (render_plan(plan), reply_id))
    return "plan ready"


# --------------------------------------------------------------------------- LinkedIn posts
class Post(BaseModel):
    kind: str = Field(description="news | industry | build")
    text: str = Field(description="120-230 words, plain text, short paragraphs, at most 3 hashtags at the end")
    sources: list[str] = Field(description="URLs of the news items the post relies on; empty if none")
    proof_id: str = Field(description="id of the proof point used, or empty")
    check: str = Field(description="What he must check before posting: facts to verify, and any opinion in the "
                                   "post that is NOT in his opinions list (say which sentence)")


class Posts(BaseModel):
    posts: list[Post]


AI_NEWS_QUERIES = ["AI agents", "large language model", "OpenAI OR Anthropic OR Gemini", "AI automation business",
                   "AI regulation", "open source AI model"]


def ai_news(days: int = 7, limit: int = 18) -> list[dict]:
    """This week's AI headlines: Google News RSS for a few queries plus the most-upvoted AI stories on
    Hacker News. Free, no keys. Each: title, url, source, date."""
    import requests
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    from .sources import _parse_time
    cfg = config.profile().get("linkedin") or {}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    seen, out = set(), []

    def add(title, url, source, when):
        key = re.sub(r"\W+", " ", title.lower())[:60]
        if title and url and when and when >= since and key not in seen:
            seen.add(key)
            out.append({"title": title.strip(), "url": url, "source": source, "date": when.date().isoformat()})
    try:
        hits = requests.get("https://hn.algolia.com/api/v1/search_by_date", timeout=20, params={
            "tags": "story", "query": "AI", "hitsPerPage": 60,
            "numericFilters": f"created_at_i>{int(since.timestamp())},points>120"}).json().get("hits", [])
        for h in sorted(hits, key=lambda h: -(h.get("points") or 0))[:8]:
            add(h.get("title", ""), h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                f"Hacker News ({h.get('points')} points)", _parse_time(h.get("created_at_i")))
    except (requests.RequestException, ValueError):
        pass
    for q in cfg.get("news_queries") or AI_NEWS_QUERIES:
        try:
            r = requests.get(f"https://news.google.com/rss/search?q={quote(q + ' when:7d')}&hl=en-US&gl=US&ceid=US:en",
                             timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            for item in list(ET.fromstring(r.content).iter("item"))[:4]:
                title = re.sub(r"\s+-\s+[^-]+$", "", item.findtext("title") or "")
                add(title, item.findtext("link") or "", item.findtext("source") or "Google News",
                    _parse_time(item.findtext("pubDate")))
        except (requests.RequestException, ET.ParseError):
            continue
    return out[:limit]


def _posts_system(p: dict) -> str:
    cfg = p.get("linkedin") or {}
    opinions = cfg.get("opinions") or []
    topics = cfg.get("topics") or ["AI agents and automation in real businesses", "what working in AI research teaches",
                                   "where the AI industry is heading", "building reliable LLM systems"]
    return (
        f"You ghost-write LinkedIn posts for {p['name']}, an AI researcher at Caudal AI who also builds AI and "
        "automation systems for businesses. The posts are about AI and automation in general: this week's news, "
        "where the industry is going, and lessons from building. The audience: founders, engineers and hiring "
        "managers in tech, who should come away thinking he understands AI deeply and ships real systems.\n\n"
        "WHO HE IS (only these facts)\n" + "\n".join(f"- {x}" for x in p["identity"]) +
        "\n\nHIS OPINIONS (use these as his views; quote their substance, sharpen the wording)\n" +
        ("\n".join(f"- {x}" for x in opinions) or "- (none written yet: propose a clear, defensible stance and flag "
                                                 "every opinion sentence in `check`)") +
        "\n\nTOPICS HE CARES ABOUT\n" + "\n".join(f"- {x}" for x in topics) +
        "\n\nTHE THREE POSTS\n"
        "1. kind=news: pick the ONE news item below that he can say something genuinely useful about. Say what "
        "happened using only the headline's facts, then his take: what it means for people building with AI, what "
        "most coverage misses, or what he'd do differently. Put its URL in `sources`.\n"
        "2. kind=industry: an opinion about the AI industry (hype vs reality, agents, evaluation, open vs closed "
        "models, AI jobs, how AI research turns into products...), argued from his opinions and from the perspective "
        "of someone who works in AI research. It may draw on a second news item; cite it if so.\n"
        "3. kind=build: one practical lesson from building AI or automation systems (reliability, evaluation, "
        "prompts, data quality, integrations, cost, when not to use an LLM). It may use one proof point below as the "
        "example; quote its numbers exactly.\n\n"
        "RULES\n"
        "- These are general AI and automation posts. At most ONE of the three may mention real estate, and only as "
        "an example, never as the topic.\n"
        "- Caudal AI: he may say he works as an AI researcher there and speak from that experience in general terms. "
        "NEVER describe Caudal AI's projects, products, clients, data, models, results or internal views, and never "
        "present an opinion as Caudal AI's. When in doubt, leave Caudal out.\n"
        "- News facts come only from the headlines given; never add numbers, dates, quotes or details that aren't "
        "there. If a headline is too thin to be sure what happened, pick another one.\n"
        "- Never invent achievements, clients, numbers or results for him. Proof-point numbers are quoted exactly.\n"
        "- Every opinion must be one he'd defend in a conversation; anything not in HIS OPINIONS goes in `check`.\n"
        "- First line: a concrete claim or observation, no clickbait, no question-bait. 4-7 short paragraphs "
        "separated by a blank line. First person. Plain words, no hype, no 'game-changer', no 'I'm thrilled', no "
        "emojis, no engagement bait ('Agree?'), at most 3 hashtags at the end.")


def linkedin_posts() -> str:
    p = config.profile()
    with db.connect() as conn:
        used = json.loads(db.get_state(conn, "content:used", "[]"))
    news = ai_news()
    proofs = sorted(p["proof_points"], key=lambda x: used.index(x["id"]) if x["id"] in used else -1)[:4]
    prompt = ("THIS WEEK'S AI NEWS (title | source | date | url)\n" +
              ("\n".join(f"- {n['title']} | {n['source']} | {n['date']} | {n['url']}" for n in news)
               or "- (no news fetched: write the news post about a general AI trend instead, with no specific "
                  "claims about recent events, and say so in `check`)") +
              "\n\nPROOF POINTS (for the build post; use at most one)\n" +
              "\n".join(f"- [{x['id']}] {x['text']}" for x in proofs) +
              "\n\nWrite the three posts: news, industry, build.")
    out = llm.generate(_posts_system(p), prompt, Posts, kind="community", temperature=0.7)
    if not out or not out.posts:
        return "error: the AI returned nothing usable"
    known = {n["url"] for n in news}
    posts = []
    for x in out.posts[:3]:
        d = x.model_dump()
        d["sources"] = [u for u in d["sources"] if u in known]   # only links we actually gave it
        d["news"] = [n for n in news if n["url"] in d["sources"]]
        posts.append(d)
    with db.connect() as conn:
        db.set_state(conn, "content:linkedin_posts", json.dumps({"at": db.now(), "posts": posts}))
        ids = [x["proof_id"] for x in posts if x["proof_id"]]
        db.set_state(conn, "content:used", json.dumps([u for u in used if u not in ids] + ids))
    return f"{len(posts)} LinkedIn post drafts ready ({len(news)} news items read)"


# --------------------------------------------------------------------------- weekly digest
def suggestions(conn) -> list[str]:
    tips = []
    segs = [r for r in report.funnel(conn, "segment") if r["name"] != "ALL"]
    best = max((r for r in segs if r["sent"] >= 20), key=lambda r: r["positive_rate"] or 0, default=None)
    for r in segs:
        if r["sent"] >= 40 and (r["reply_rate"] or 0) < 2:
            move = f" or move its daily quota to {best['name']}" if best and best["name"] != r["name"] else ""
            tips.append(f"{r['name']}: {r['sent']} sent, {r['reply_rate'] or 0}% replies. Change its angle or offer{move}.")
    by_seg: dict[str, list[dict]] = {}
    for a in report.angles(conn):
        by_seg.setdefault(a["segment"], []).append(a)
    for seg, rows in by_seg.items():
        ready = [a for a in rows if a["sent"] >= 25]
        if len(ready) >= 2:
            ready.sort(key=lambda a: a["reply_rate"] or 0, reverse=True)
            top, low = ready[0], ready[-1]
            if (top["reply_rate"] or 0) - (low["reply_rate"] or 0) >= 1.5:
                tips.append(f"{seg}: angle '{top['angle']}' gets {top['reply_rate']}% replies vs '{low['angle']}' "
                            f"{low['reply_rate']}%. Keep '{top['angle']}' and replace '{low['angle']}' in settings.yaml.")
    waiting = conn.execute(f"SELECT COUNT(*) FROM replies WHERE handled=0 AND category IN {report.NEEDS_YOU_SQL}").fetchone()[0]
    if waiting:
        tips.append(f"{waiting} repl{'y is' if waiting == 1 else 'ies are'} still waiting on you. Answer today.")
    stale = conn.execute("SELECT company FROM leads WHERE deal_stage='proposal_sent' AND "
                         "((deal_next_due != '' AND deal_next_due < ?) OR "
                         "(deal_next_due = '' AND deal_updated < ?))",
                         (date.today().isoformat(),
                          (datetime.now(timezone.utc) - timedelta(days=5)).isoformat())).fetchall()
    if stale:
        tips.append("Proposals with no answer for 5+ days, follow up: " + ", ".join(r[0] for r in stale[:5]) + ".")
    return tips or ["Nothing to change yet: keep the volume steady until each segment has about 40 sends."]


def weekly_digest() -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=7))
    day_since = since.date().isoformat()
    with db.connect() as conn:
        first = conn.execute("SELECT COALESCE(SUM(count),0) FROM send_log WHERE kind='first' AND day>=?", (day_since,)).fetchone()[0]
        follow = conn.execute("SELECT COALESCE(SUM(count),0) FROM send_log WHERE kind!='first' AND day>=?", (day_since,)).fetchone()[0]
        replied = conn.execute("SELECT COUNT(*) FROM replies WHERE received_at>=? AND category NOT IN ('bounce','out_of_office')",
                               (since.isoformat(),)).fetchone()[0]
        positive = conn.execute(f"SELECT COUNT(*) FROM replies WHERE received_at>=? AND category IN {report.POSITIVE_SQL}",
                                (since.isoformat(),)).fetchone()[0]
        moved = {s: conn.execute("SELECT COUNT(*) FROM leads WHERE deal_stage=? AND deal_updated>=?",
                                 (s, since.isoformat())).fetchone()[0] for s in STAGES}
        won_values = {r[0]: r[1] for r in conn.execute("SELECT COALESCE(deal_currency, 'USD'), "
                      "SUM(deal_value) FROM leads WHERE deal_stage='won' GROUP BY 1") if r[1]}
        tips = suggestions(conn)
    text = "\n".join([
        "Outreach: last 7 days",
        f"Sent: {first} first emails, {follow} follow-ups",
        f"Replies: {replied} ({positive} positive)",
        f"Deals: {moved['call_booked']} calls booked, {moved['proposal_sent']} proposals, {moved['won']} won"
        + (" (total won so far: " + ", ".join(f"{c} {v:g}" for c, v in sorted(won_values.items())) + ")"
           if won_values else ""),
        "", "This week:", *[f"- {t}" for t in tips]])
    with db.connect() as conn:
        db.set_state(conn, "digest:last", json.dumps({"at": db.now(), "text": text}))
    replies.notify(text)
    return "digest sent"
