"""Reads replies over IMAP, stops sequences, classifies each reply, and drafts your answer.

For every interested reply it saves a threaded draft in the inbox's Drafts folder, so the only
thing left for you is to read it, adjust it and press send (reply within the hour: speed to
reply is the biggest single lever on converting a reply into a call).
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import time as _time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime
from typing import Literal

import anthropic
import requests
from pydantic import BaseModel, Field

from . import config, db

BOUNCE_FROM = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
POSITIVE = {"interested", "meeting_request", "question", "referral"}


class ReplyClass(BaseModel):
    category: Literal["interested", "meeting_request", "question", "referral", "not_now",
                      "not_interested", "unsubscribe", "out_of_office", "bounce", "other"]
    summary: str = Field(description="One line: what they said and what they want")
    suggested_reply: str = Field(description="Plain-text reply to send, under 90 words, no signature; empty for bounce/unsubscribe/out_of_office")


CLASSIFY_SYSTEM = """\
You triage replies to {name}'s cold emails and draft the answer.
Facts about {name}: {identity}
Booking link: {calendar}. Rates: {rates}.
Categories: interested (wants to know more), meeting_request (proposes or accepts a call),
question (asks about price/scope/experience), referral (points to another person),
not_now (timing), not_interested, unsubscribe (asks to stop), out_of_office, bounce, other.
Suggested reply rules: answer exactly what they asked; for interest or questions, propose two
specific 20-minute slots (weekday, IST and their local time if abroad) AND include the booking
link; for price questions give the rate range and offer a fixed-price scoped first milestone;
for referral thank them and ask for the intro or the email; for not_now ask if it is ok to
check back in a specific month. Never over-promise, never invent experience."""


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    return (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "replace")


def _strip_quoted(text: str) -> str:
    out = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()[:4000]


def _match_lead(conn, from_addr: str, refs: str, body: str):
    lead = conn.execute("SELECT * FROM leads WHERE lower(email)=?", (from_addr.lower(),)).fetchone()
    if lead:
        return lead
    for mid in re.findall(r"<[^>]+>", refs or ""):
        row = conn.execute("SELECT l.* FROM messages m JOIN leads l ON l.id=m.lead_id WHERE m.message_id=?", (mid,)).fetchone()
        if row:
            return row
    domain = from_addr.split("@")[-1].lower()
    lead = conn.execute("SELECT * FROM leads WHERE domain=? AND status IN ('active','finished')", (domain,)).fetchone()
    if lead:
        return lead  # a colleague replied or the email was forwarded
    for addr in EMAIL_RE.findall(body or ""):  # bounces quote the original recipient
        lead = conn.execute("SELECT * FROM leads WHERE lower(email)=? AND status IN ('active','finished')",
                            (addr.lower(),)).fetchone()
        if lead:
            return lead
    return None


def classify(client, lead, subject: str, body: str) -> ReplyClass:
    p = config.profile()
    system = CLASSIFY_SYSTEM.format(name=p["name"], identity="; ".join(p["identity"]),
                                    calendar=p.get("calendar_link", ""), rates=p.get("rates", ""))
    response = client.messages.parse(
        model=config.model(),
        max_tokens=4000,
        output_config={"effort": "low"},
        system=system,
        messages=[{"role": "user", "content":
                   f"Lead: {lead['first_name']} {lead['last_name']}, {lead['title']} at {lead['company']} "
                   f"({lead['country']}), segment {lead['segment']}.\nSubject: {subject}\n\nReply:\n{body}"}],
        output_format=ReplyClass,
    )
    if response.parsed_output is None:
        return ReplyClass(category="other", summary="(could not classify)", suggested_reply="")
    return response.parsed_output


def _save_draft(imap: imaplib.IMAP4_SSL, box: dict, lead, subject: str, in_reply_to: str, body: str) -> None:
    em = EmailMessage()
    em["From"] = formataddr((box.get("display_name") or config.profile()["name"], box["email"]))
    em["To"] = lead["email"]
    em["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    em["In-Reply-To"] = in_reply_to
    em["References"] = in_reply_to
    em["Message-ID"] = make_msgid(domain=box["email"].split("@")[1])
    em.set_content(body + "\n\n" + config.profile()["signatures"]["short"] + "\n")
    folder = box.get("drafts_folder", "[Gmail]/Drafts")
    imap.append(f'"{folder}"', r"(\Draft)", imaplib.Time2Internaldate(_time.time()), em.as_bytes())


def notify(text: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:3500]}, timeout=10)
        except requests.RequestException:
            pass


def sync(days: int = 4, use_mock: bool = False) -> int:
    client = None if use_mock else anthropic.Anthropic()
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    handled = 0
    for box in config.inboxes():
        if not box.get("imap_host") or not box["password"]:
            continue
        imap = imaplib.IMAP4_SSL(box["imap_host"], int(box.get("imap_port", 993)))
        imap.login(box.get("username", box["email"]), box["password"])
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "SINCE", since)
        for uid in data[0].split():
            _, fetched = imap.fetch(uid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(fetched[0][1])
            mid = msg.get("Message-ID", f"<uid-{uid.decode()}@{box['email']}>")
            from_addr = parseaddr(msg.get("From", ""))[1]
            if from_addr.lower() == box["email"].lower():
                continue
            with db.connect() as conn:
                if conn.execute("SELECT 1 FROM replies WHERE message_id=?", (mid,)).fetchone():
                    continue
                raw_body = _body_text(msg)
                is_bounce = bool(BOUNCE_FROM.search(msg.get("From", "")))
                lead = _match_lead(conn, from_addr, (msg.get("In-Reply-To", "") + " " + msg.get("References", "")),
                                   raw_body if is_bounce else "")
                if not lead:
                    continue
                body = _strip_quoted(raw_body)
                subject = msg.get("Subject", "")
                if is_bounce:
                    result = ReplyClass(category="bounce", summary="hard bounce", suggested_reply="")
                elif use_mock:
                    result = ReplyClass(category="other", summary="(mock)", suggested_reply="")
                else:
                    result = classify(client, lead, subject, body)
                try:
                    received = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError):
                    received = db.now()
                conn.execute(
                    """INSERT INTO replies (lead_id, inbox, imap_uid, message_id, from_addr, subject, body,
                       received_at, category, summary, suggested_reply) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (lead["id"], box["email"], uid.decode(), mid, from_addr, subject, body, received,
                     result.category, result.summary, result.suggested_reply))
                _apply(conn, lead, result)
                conn.commit()
            handled += 1
            print(f"  {result.category:<16} {lead['email']:<38} {result.summary}")
            if result.category in POSITIVE and result.suggested_reply:
                _save_draft(imap, box, lead, subject, mid, result.suggested_reply)
                notify(f"🔥 {result.category.upper()} from {lead['first_name']} @ {lead['company']}\n"
                       f"{result.summary}\n\nDraft reply saved in {box['email']} Drafts.")
        imap.logout()
    return handled


def _apply(conn, lead, result: ReplyClass) -> None:
    if result.category == "out_of_office":
        return  # keep the sequence running
    conn.execute("UPDATE messages SET status='cancelled' WHERE lead_id=? AND status IN ('approved','draft')", (lead["id"],))
    if result.category == "bounce":
        db.set_lead(conn, lead["id"], status="bounced", email_status="invalid")
        db.suppress(conn, lead["email"], "bounce")
    elif result.category in ("unsubscribe", "not_interested"):
        db.set_lead(conn, lead["id"], status="unsubscribed")
        db.suppress(conn, lead["email"], result.category)
    else:
        db.set_lead(conn, lead["id"], status="replied")
