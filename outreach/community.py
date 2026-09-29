"""Fresh "[Hiring]" posts from communities where people ask for exactly this work.

These people want an answer on the post or a DM, not a cold email, and the first few good
replies usually win. So this doesn't feed the email pipeline. It:
  1. pulls new posts (Discourse forums such as the n8n community Jobs board; Reddit r/forhire),
  2. has Gemini check each one against your profile and draft a short, specific reply,
  3. writes data/opportunities_today.md and pings Telegram for each relevant post.
You then reply by hand within the hour. Run it from cron every 30 minutes.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from pydantic import BaseModel

from . import config, db, llm, replies
from .prospect import _clean
from .sources import _parse_time

UA = {"User-Agent": "agent-outreach/1.0 (personal job alerts)"}
ATOM = "{http://www.w3.org/2005/Atom}"


class PostCheck(BaseModel):
    relevant: bool
    reason: str      # one line: why it is or isn't a fit
    reply: str       # the reply/DM to send if relevant, else ""


# --------------------------------------------------------------------------- fetchers
def fetch_discourse(src: dict) -> list[dict]:
    """Latest topics in one Discourse category, e.g. https://community.n8n.io + category 'jobs'."""
    base = src["url"].rstrip("/")
    topics = requests.get(f"{base}/c/{src['category']}/l/latest.json", headers=UA, timeout=30).json()
    out = []
    for t in topics.get("topic_list", {}).get("topics", []):
        if t.get("pinned") or t.get("closed"):
            continue
        out.append({"ext_id": str(t["id"]), "title": t.get("title", ""),
                    "url": f"{base}/t/{t.get('slug', 'topic')}/{t['id']}",
                    "author": "", "body": "", "posted": _parse_time(t.get("created_at")),
                    "_detail": f"{base}/t/{t['id']}.json"})
    return out


def discourse_body(post: dict) -> None:
    try:
        d = requests.get(post["_detail"], headers=UA, timeout=30).json()
        first = d["post_stream"]["posts"][0]
        post["body"] = _clean(first.get("cooked", ""))
        post["author"] = first.get("username", "")
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass


def _reddit_token() -> str:
    cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return ""
    r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret), headers=UA,
                      data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    return r.json().get("access_token", "")


def parse_reddit_atom(xml_text: str) -> list[dict]:
    out = []
    for e in ET.fromstring(xml_text).iter(f"{ATOM}entry"):
        link = e.find(f"{ATOM}link")
        out.append({"ext_id": (e.findtext(f"{ATOM}id") or "").split("_")[-1],
                    "title": e.findtext(f"{ATOM}title") or "",
                    "url": link.get("href") if link is not None else "",
                    "author": (e.findtext(f"{ATOM}author/{ATOM}name") or "").removeprefix("/u/"),
                    "body": _clean(e.findtext(f"{ATOM}content") or ""),
                    "posted": _parse_time(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"))})
    return out


def fetch_reddit(src: dict) -> list[dict]:
    """Official API when REDDIT_CLIENT_ID/SECRET are set (free 'script' app); public RSS otherwise."""
    sub = src["subreddit"]
    token = _reddit_token()
    if token:
        data = requests.get(f"https://oauth.reddit.com/r/{sub}/new", params={"limit": 100}, timeout=30,
                            headers={**UA, "Authorization": f"bearer {token}"}).json()
        return [{"ext_id": c["data"]["id"], "title": c["data"].get("title", ""),
                 "url": "https://www.reddit.com" + c["data"].get("permalink", ""),
                 "author": c["data"].get("author", ""), "body": c["data"].get("selftext", ""),
                 "posted": _parse_time(c["data"].get("created_utc"))}
                for c in data.get("data", {}).get("children", [])]
    r = requests.get(f"https://www.reddit.com/r/{sub}/new/.rss", params={"limit": 100}, headers=UA, timeout=30)
    r.raise_for_status()
    return parse_reddit_atom(r.text)


FETCHERS = {"discourse": fetch_discourse, "reddit": fetch_reddit}


# --------------------------------------------------------------------------- pipeline
def wanted(post: dict, src: dict) -> bool:
    if src.get("title_regex") and not re.search(src["title_regex"], post["title"], re.I):
        return False
    blob = f"{post['title']} {post['body']}"
    if src.get("include_regex") and post["body"] and not re.search(src["include_regex"], blob, re.I):
        return False
    if any(x.lower() in blob.lower() for x in src.get("exclude_any", [])):
        return False
    posted = post.get("posted")
    return not posted or posted > datetime.now(timezone.utc) - timedelta(hours=src.get("max_age_hours", 48))


def _system() -> str:
    p = config.profile()
    proofs = "\n".join(f"- {x['text']}" for x in p["proof_points"])
    who = "\n".join(f"- {x}" for x in p["identity"])
    return (
        "You screen freelance job posts for one person and draft his reply.\n\n"
        f"WHO HE IS\n{who}\nRates: {p.get('rates', '')}\nDelivery: {p.get('delivery_promise', '')}\n\n"
        f"PROOF POINTS (the only achievements you may mention)\n{proofs}\n\n"
        "relevant = true only if the poster is paying for work he can clearly do (automation, WhatsApp/CRM/"
        "lead handling, AI/LLM features, web apps, scrapers, dashboards, integrations) and nothing rules him "
        "out (must be local/onsite, a citizenship or visa requirement, a senior full-time hire, unpaid or "
        "equity-only work, or the post is someone offering their own services).\n"
        "reply: 60-110 words, plain text, written to the poster. Open with their specific problem in your own "
        "words, give the single most similar proof point, say how he'd approach it and how fast a first "
        "version could be ready, and end with one question that moves it forward. No greetings like 'I hope "
        "you are well', no flattery, no links, no invented facts. Empty string if not relevant."
    )


def check(post: dict, use_mock: bool) -> PostCheck:
    if use_mock:
        return PostCheck(relevant=True, reason="mock", reply=f"(mock reply to: {post['title']})")
    prompt = f"SOURCE: {post['source']}\nTITLE: {post['title']}\n\nPOST:\n{post['body'][:4000] or '(title only)'}"
    return llm.generate(_system(), prompt, PostCheck, kind="reply", temperature=0.4) or \
        PostCheck(relevant=False, reason="could not check", reply="")


def run(use_mock: bool = False) -> int:
    """Fetch, screen and store new posts; returns how many relevant new ones were found."""
    found = 0
    for src in config.settings().get("community", []):
        if not src.get("enabled", True):
            continue
        name = src.get("name") or src.get("subreddit") or src.get("url")
        try:
            posts = FETCHERS[src["type"]](src)
        except (requests.RequestException, ValueError, ET.ParseError, KeyError) as e:
            print(f"  {name}: error: {e}")
            continue
        new = 0
        for post in posts:
            post["source"] = name
            with db.connect() as conn:
                if conn.execute("SELECT 1 FROM posts WHERE source=? AND ext_id=?",
                                (name, post["ext_id"])).fetchone():
                    continue
            if src["type"] == "discourse" and wanted({**post, "body": ""}, src):
                discourse_body(post)
            if not wanted(post, src):
                with db.connect() as conn:  # remember it so it isn't re-checked
                    conn.execute("INSERT OR IGNORE INTO posts (source, ext_id, title, url, found_at, relevant, "
                                 "status) VALUES (?,?,?,?,?,0,'done')",
                                 (name, post["ext_id"], post["title"], post["url"], db.now()))
                continue
            try:
                result = check(post, use_mock)
            except llm.QuotaExhausted:
                print("  Gemini daily quota used up; the rest waits for the next run")
                return found
            with db.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO posts (source, ext_id, title, url, author, body, posted_at, found_at, "
                    "relevant, reason, draft_reply, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, post["ext_id"], post["title"], post["url"], post["author"], post["body"][:6000],
                     post["posted"].isoformat() if post["posted"] else "", db.now(), int(result.relevant),
                     result.reason, result.reply, "new" if result.relevant else "done"))
            if result.relevant:
                new += 1
                replies.notify(f"New lead on {name}: {post['title']}\n{post['url']}\n\nDraft reply:\n{result.reply}")
                with db.connect() as conn:
                    conn.execute("UPDATE posts SET status='notified' WHERE source=? AND ext_id=?",
                                 (name, post["ext_id"]))
        print(f"  {name}: {len(posts)} posts, {new} new relevant")
        found += new
    write_digest(config.DATA_DIR / "opportunities_today.md")
    return found


def write_digest(path) -> int:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM posts WHERE relevant=1 AND status != 'done' "
                            "ORDER BY COALESCE(NULLIF(posted_at, ''), found_at) DESC LIMIT 40").fetchall()
    lines = ["# Community posts to answer (newest first)\n",
             "Reply by hand, ideally within the hour. Edit the draft so it sounds like you. "
             "Then run `python -m outreach post-done <id>`.\n"]
    for r in rows:
        lines += [f"## [{r['id']}] {r['title']}", f"- Where: {r['source']} - {r['url']}",
                  f"- Posted: {r['posted_at'] or 'unknown'}  |  why: {r['reason']}", "",
                  r["draft_reply"] or "", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return len(rows)
