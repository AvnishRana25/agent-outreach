"""Reads replies, stops sequences, classifies each reply with Gemini, and drafts your answer.

Gmail/IMAP inboxes: the drafted answer is saved in the mailbox's Drafts folder, threaded.
Zoho API inboxes: run `python -m outreach reply <id>` to edit and send it from the terminal.
Either way you get a Telegram ping, if configured.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Literal

import requests
from pydantic import BaseModel, Field

from . import config, db, llm, transport

BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
POSITIVE = {"interested", "meeting_request", "question", "referral"}


class ReplyClass(BaseModel):
    category: Literal["interested", "meeting_request", "question", "referral", "not_now",
                      "not_interested", "unsubscribe", "out_of_office", "bounce", "other"]
    summary: str = Field(description="One line: what they said and what they want")
    suggested_reply: str = Field(description="Plain-text reply under 90 words, no signature; empty when not needed")


def _system() -> str:
    p = config.profile()
    return llm.load_prompt("reply_system.md").format(
        name=p["name"], identity="; ".join(p["identity"]),
        calendar=p.get("calendar_link", ""), rates=p.get("rates", ""))


def _strip_quoted(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()[:4000]


def _match_lead(conn, msg: transport.Incoming, is_bounce: bool):
    lead = conn.execute("SELECT * FROM leads WHERE lower(email)=?", (msg.from_addr.lower(),)).fetchone()
    if lead:
        return lead
    for mid in re.findall(r"<[^>]+>", msg.refs or ""):
        row = conn.execute("SELECT l.* FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.message_id=?",
                           (mid,)).fetchone()
        if row:
            return row
    domain = msg.from_addr.split("@")[-1].lower()
    lead = conn.execute("SELECT * FROM leads WHERE domain=? AND domain != '' AND status IN ('active','finished')",
                        (domain,)).fetchone()
    if lead:
        return lead  # a colleague replied, or it was forwarded
    if is_bounce:  # bounce notices quote the original recipient
        for addr in EMAIL_RE.findall(msg.body or ""):
            lead = conn.execute("SELECT * FROM leads WHERE lower(email)=? AND status IN ('active','finished')",
                                (addr.lower(),)).fetchone()
            if lead:
                return lead
    return None


def classify(lead, subject: str, body: str) -> ReplyClass:
    prompt = (f"Lead: {lead['first_name']} {lead['last_name']}, {lead['title']} at {lead['company']} "
              f"({lead['country']}), segment {lead['segment']}.\nSubject: {subject}\n\nReply:\n{body}")
    result = llm.generate(_system(), prompt, ReplyClass, kind="reply", temperature=0.3)
    return result or ReplyClass(category="other", summary="(could not classify)", suggested_reply="")


def notify(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:3500]}, timeout=10)
        except requests.RequestException:
            pass


def telegram_setup() -> None:
    """Find the numeric chat id Telegram needs (a @username only works for public channels)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json()
    if not r.get("ok"):
        raise SystemExit(f"Telegram rejected the token: {r.get('description')}")
    chats = {}
    for u in r.get("result", []):
        chat = (u.get("message") or u.get("edited_message") or {}).get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat
    if not chats:
        print("No messages yet. Open your bot in Telegram, press Start, send it any message, then rerun this.")
        return
    for cid, chat in chats.items():
        who = chat.get("username") or chat.get("first_name") or chat.get("title")
        print(f"@{who}: TELEGRAM_CHAT_ID={cid}")
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=10,
                      json={"chat_id": cid, "text": "agent-outreach is connected. Put this chat id in .env: "
                                                    f"TELEGRAM_CHAT_ID={cid}"})
    print("Copy the TELEGRAM_CHAT_ID line for your account into .env.")


def sync(days: int = 4, use_mock: bool = False) -> int:
    handled = 0
    for box in config.inboxes():
        try:
            incoming = transport.fetch(box, days)
        except (transport.ZohoError, OSError, requests.RequestException) as e:
            print(f"  ! could not read {box['email']}: {e}")
            continue
        for msg in incoming:
            if msg.from_addr.lower() == box["email"].lower():
                continue
            with db.connect() as conn:
                if conn.execute("SELECT 1 FROM replies WHERE message_id=?", (msg.message_id,)).fetchone():
                    continue
                is_bounce = bool(BOUNCE_FROM.search(msg.from_header))
                lead = _match_lead(conn, msg, is_bounce)
                if not lead:
                    continue
                body = _strip_quoted(msg.body)
                if is_bounce:
                    result = ReplyClass(category="bounce", summary="hard bounce", suggested_reply="")
                elif use_mock:
                    result = ReplyClass(category="other", summary="(mock)", suggested_reply="")
                else:
                    try:
                        result = classify(lead, msg.subject, body)
                    except llm.QuotaExhausted:
                        result = ReplyClass(category="other", summary="(quota reached; read it yourself)",
                                            suggested_reply="")
                conn.execute(
                    """INSERT INTO replies (lead_id, inbox, imap_uid, message_id, from_addr, subject, body,
                       received_at, category, summary, suggested_reply) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (lead["id"], box["email"], msg.provider_id, msg.message_id, msg.from_addr, msg.subject,
                     body, msg.received_at, result.category, result.summary, result.suggested_reply))
                reply_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                _apply(conn, lead, result)
            handled += 1
            print(f"  {result.category:<16} {lead['email']:<38} {result.summary}")
            if result.category in POSITIVE:
                where = "no reply drafted"
                if result.suggested_reply:
                    try:
                        saved = transport.save_draft(box, lead["email"], msg.subject, msg.message_id,
                                                     result.suggested_reply + "\n\n" + config.profile()["signatures"]["short"])
                    except OSError:
                        saved = False
                    where = f"draft saved in {box['email']}" if saved else f"send with: python -m outreach reply {reply_id}"
                notify(f"🔥 {result.category.upper()} from {lead['first_name'] or ''} @ {lead['company']}\n"
                       f"{result.summary}\n\n{where}")
    return handled


def _apply(conn, lead, result: ReplyClass) -> None:
    if result.category == "out_of_office":
        return
    conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status IN ('approved','draft')",
                 (lead["id"],))
    if result.category == "bounce":
        db.set_lead(conn, lead["id"], status="bounced", email_status="invalid")
        db.suppress(conn, lead["email"], "bounce")
    elif result.category in ("unsubscribe", "not_interested"):
        db.set_lead(conn, lead["id"], status="unsubscribed")
        db.suppress(conn, lead["email"], result.category)
    else:
        db.set_lead(conn, lead["id"], status="replied")


def send_reply(reply_id: int) -> None:
    """Open the suggested answer in $EDITOR, then send it threaded from the same inbox."""
    with db.connect() as conn:
        r = conn.execute("SELECT r.*, l.email AS lead_email FROM replies r JOIN leads l ON l.id=r.lead_id "
                         "WHERE r.id=?", (reply_id,)).fetchone()
    if not r:
        raise SystemExit(f"no reply #{reply_id}")
    print(f"From {r['from_addr']}: {r['subject']}\n\n{r['body']}\n")
    draft = (r["suggested_reply"] or "") + "\n\n" + config.profile()["signatures"]["short"]
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as f:
        f.write(draft)
        path = f.name
    subprocess.call([os.environ.get("EDITOR", "nano"), path])
    body = open(path).read().strip()
    os.unlink(path)
    if not body or input("Send this reply? [y/N] ").strip().lower() != "y":
        print("not sent")
        return
    deliver_reply(reply_id, body)
    print("sent")


def deliver_reply(reply_id: int, body: str) -> None:
    """Send `body` as a threaded answer to reply #id from the inbox it arrived in, and mark it handled."""
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM replies WHERE id=?", (reply_id,)).fetchone()
    if not r:
        raise ValueError(f"no reply #{reply_id}")
    box = config.inbox(r["inbox"])
    subject = r["subject"] if (r["subject"] or "").lower().startswith("re:") else "Re: " + (r["subject"] or "")
    transport.send(box, r["from_addr"], subject, body,
                   {"message_id": r["message_id"], "provider_id": r["imap_uid"]})
    with db.connect() as conn:
        conn.execute("UPDATE replies SET handled=1 WHERE id=?", (reply_id,))
