"""How mail actually leaves and arrives: SMTP/IMAP (Gmail, paid Zoho) or the Zoho Mail REST API.

Zoho's free plan is webmail-only: IMAP/POP/SMTP are paid features. Its REST API may or may not
be enabled on your plan, so run `python -m outreach zoho-check` first. If it fails, send from
Gmail over SMTP (free, with an app password) instead.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr, parsedate_to_datetime

import requests

from . import config


@dataclass
class Incoming:
    message_id: str
    from_addr: str
    from_header: str
    subject: str
    body: str
    received_at: str
    refs: str = ""          # In-Reply-To + References headers
    provider_id: str = ""   # Zoho message id, needed to reply through the API


def _display(box: dict) -> str:
    return box.get("display_name") or config.profile()["name"]


# --------------------------------------------------------------------------- SMTP / IMAP
def smtp_send(box: dict, to: str, subject: str, body: str, thread: dict | None,
              custom_msg_id: str | None = None) -> tuple[str, str]:
    em = EmailMessage()
    em["From"] = formataddr((_display(box), box["email"]))
    em["To"] = to
    em["Subject"] = subject
    em["Date"] = formatdate(localtime=True)
    domain = box["email"].split("@")[1]
    if custom_msg_id:
        em["Message-ID"] = f"<{custom_msg_id}@{domain}>" if not custom_msg_id.startswith("<") else custom_msg_id
    else:
        em["Message-ID"] = make_msgid(domain=domain)
    if thread and thread.get("message_id"):
        em["In-Reply-To"] = thread["message_id"]
        em["References"] = thread["message_id"]
    em["List-Unsubscribe"] = f"<mailto:{box['email']}?subject=unsubscribe>"
    em.set_content(body)
    ctx = ssl.create_default_context()
    port = int(box.get("smtp_port", 465))
    user = box.get("username", box["email"])
    if port == 465:
        with smtplib.SMTP_SSL(box["smtp_host"], port, context=ctx, timeout=30) as s:
            s.login(user, box["password"])
            s.send_message(em)
    else:
        with smtplib.SMTP(box["smtp_host"], port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, box["password"])
            s.send_message(em)
    return em["Message-ID"], ""


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return re.sub(r"<[^>]+>", " ", (part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
        return ""
    return (msg.get_payload(decode=True) or b"").decode(msg.get_content_charset() or "utf-8", "replace")


def imap_fetch(box: dict, days: int) -> list[Incoming]:
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(box["imap_host"], int(box.get("imap_port", 993)))
    imap.login(box.get("username", box["email"]), box["password"])
    imap.select("INBOX", readonly=True)
    _, data = imap.search(None, "SINCE", since)
    out = []
    for uid in data[0].split():
        _, fetched = imap.fetch(uid, "(BODY.PEEK[])")
        msg = email.message_from_bytes(fetched[0][1])
        try:
            received = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            received = datetime.now(timezone.utc).isoformat()
        out.append(Incoming(
            message_id=msg.get("Message-ID", f"<uid-{uid.decode()}@{box['email']}>"),
            from_addr=parseaddr(msg.get("From", ""))[1], from_header=msg.get("From", ""),
            subject=msg.get("Subject", ""), body=_body_text(msg), received_at=received,
            refs=f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"))
    imap.logout()
    return out


def imap_save_draft(box: dict, to: str, subject: str, in_reply_to: str, body: str) -> None:
    em = EmailMessage()
    em["From"] = formataddr((_display(box), box["email"]))
    em["To"] = to
    em["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    em["In-Reply-To"] = in_reply_to
    em["References"] = in_reply_to
    em.set_content(body)
    imap = imaplib.IMAP4_SSL(box["imap_host"], int(box.get("imap_port", 993)))
    imap.login(box.get("username", box["email"]), box["password"])
    imap.append(f'"{box.get("drafts_folder", "[Gmail]/Drafts")}"', r"(\Draft)",
                imaplib.Time2Internaldate(time.time()), em.as_bytes())
    imap.logout()


# --------------------------------------------------------------------------- Zoho Mail REST API
class ZohoError(RuntimeError):
    def __init__(self, msg: str, http_status: int | None = None):
        super().__init__(msg)
        self.http_status = http_status   # set when Zoho's mail API answered with an error


class InboxUnavailable(RuntimeError):
    pass


ZOHO_HINTS = {
    "invalid_code": ("\n  ZOHO_REFRESH_TOKEN is not a valid refresh token. Usually the one-time code from "
                     "'Generate Code' was pasted instead. Generate a new code and run: "
                     "python -m outreach zoho-token <code>"),
    "invalid_client": ("\n  Client ID/secret not recognised here. Check ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET, and "
                       "that the Self Client was made at api-console.zoho.in (zoho_dc: in) - a .com client "
                       "won't work for a zohomail.in account."),
}


class Zoho:
    """Minimal Zoho Mail API client using a Self Client refresh token."""

    def __init__(self, box: dict):
        self.box = box
        dc = box.get("zoho_dc", "in")                     # "in" for zoho.in accounts, "com" otherwise
        self.accounts_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
        self.api = f"https://mail.zoho.{dc}/api"
        self.client_id = os.getenv(box.get("client_id_env", "ZOHO_CLIENT_ID"), "")
        self.client_secret = os.getenv(box.get("client_secret_env", "ZOHO_CLIENT_SECRET"), "")
        self.refresh_token = os.getenv(box.get("refresh_token_env", "ZOHO_REFRESH_TOKEN"), "")
        self._token, self._expires, self._account_id = "", 0.0, box.get("zoho_account_id", "")

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = requests.post(self.accounts_url, data={
            "refresh_token": self.refresh_token, "client_id": self.client_id,
            "client_secret": self.client_secret, "grant_type": "refresh_token"}, timeout=30)
        data = r.json()
        if "access_token" not in data:
            error = str(data.get("error", "unknown"))
            error = error if re.fullmatch(r"[a-z_]{1,50}", error) else "unknown"
            raise ZohoError(f"token refresh failed: {error}{ZOHO_HINTS.get(error, '')}")
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def exchange_code(self, code: str) -> dict:
        """Turn a Self Client grant code (valid a few minutes, usable once) into a refresh token."""
        r = requests.post(self.accounts_url, data={
            "code": code.strip(), "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "authorization_code"}, timeout=30)
        return r.json()

    def call(self, method: str, path: str, **kw) -> dict:
        r = requests.request(method, f"{self.api}{path}", timeout=45,
                             headers={"Authorization": f"Zoho-oauthtoken {self.token()}",
                                      "Accept": "application/json"}, **kw)
        try:
            data = r.json()
        except ValueError:
            raise ZohoError(f"{method} {path}: HTTP {r.status_code}", r.status_code)
        status = (data.get("status") or {}).get("code", r.status_code)
        if r.status_code >= 400 or (isinstance(status, int) and status >= 400):
            raise ZohoError(f"{method} {path}: HTTP {r.status_code}, API status {status if isinstance(status, int) else 'unknown'}",
                            r.status_code if r.status_code >= 400 else int(status))
        return data

    def account_id(self) -> str:
        if not self._account_id:
            accounts = self.call("GET", "/accounts").get("data", [])
            match = [a for a in accounts if self.box["email"].lower() in json_lower(a)]
            if not (match or accounts):
                raise ZohoError("no Zoho Mail accounts visible to this token")
            self._account_id = str((match or accounts)[0]["accountId"])
        return self._account_id

    def send(self, to: str, subject: str, body: str, thread: dict | None) -> tuple[str, str]:
        payload = {"fromAddress": self.box["email"], "toAddress": to, "subject": subject,
                   "content": body, "mailFormat": "plaintext"}
        acc = self.account_id()
        if thread and thread.get("provider_id"):
            payload["action"] = "reply"
            data = self.call("POST", f"/accounts/{acc}/messages/{thread['provider_id']}", json=payload)
        else:
            data = self.call("POST", f"/accounts/{acc}/messages", json=payload)
        d = data.get("data") or {}
        return str(d.get("mailId") or d.get("messageId") or ""), str(d.get("messageId") or "")

    def spam_folder(self) -> str:
        """Replies to cold email sometimes land in Spam; read that folder too."""
        if not hasattr(self, "_spam"):
            spam = ""
            for f in self.call("GET", f"/accounts/{self.account_id()}/folders").get("data", []):
                if str(f.get("folderType", "")).lower() == "spam" or str(f.get("folderName", "")).lower() == "spam":
                    if not f.get("folderId"):
                        raise ZohoError("Spam folder has no ID")
                    spam = str(f["folderId"])
                    break
            self._spam = spam
        return self._spam

    def fetch(self, days: int, known: set | None = None) -> list[Incoming]:
        acc = self.account_id()
        since_ms = (time.time() - days * 86400) * 1000
        rows = []
        for folder in (None, self.spam_folder()):
            if folder == "":
                continue
            start = 1
            while True:
                params = {"start": start, "limit": 200}
                if folder:
                    params["folderId"] = folder
                page = self.call("GET", f"/accounts/{acc}/messages/view", params=params).get("data", [])
                rows.extend(page)
                if len(page) < 200 or float(page[-1].get("receivedTime") or 0) < since_ms:
                    break
                start += len(page)
        out = []
        for m in rows:
            if float(m.get("receivedTime") or 0) < since_ms:
                continue
            mid, fid = m.get("messageId"), m.get("folderId")
            if known and str(mid) in known:   # already stored: skip the two content calls
                continue
            if self.box["email"].lower() in str(m.get("fromAddress", "")).lower():
                continue
            try:
                body = self.call("GET", f"/accounts/{acc}/folders/{fid}/messages/{mid}/content").get("data", {}).get("content", "")
                head = self.call("GET", f"/accounts/{acc}/folders/{fid}/messages/{mid}/header").get("data", {}).get("headerContent", "")
            except ZohoError:
                body, head = m.get("summary", ""), ""
            refs = " ".join(re.findall(r"^(?:In-Reply-To|References):(.*)$", head or "", re.M | re.I))
            hdr_mid = re.search(r"^Message-ID:\s*(<[^>]+>)", head or "", re.M | re.I)
            received = datetime.fromtimestamp(float(m.get("receivedTime", 0)) / 1000, timezone.utc).isoformat()
            out.append(Incoming(
                message_id=hdr_mid.group(1) if hdr_mid else f"<zoho-{mid}>",
                from_addr=parseaddr(m.get("fromAddress", ""))[1] or m.get("fromAddress", ""),
                from_header=m.get("sender", "") + " " + m.get("fromAddress", ""),
                subject=m.get("subject", ""), body=re.sub(r"<[^>]+>", " ", body or ""),
                received_at=received, refs=refs, provider_id=str(mid)))
        return out


def json_lower(obj) -> str:
    return str(obj).lower()


# --------------------------------------------------------------------------- dispatch
_zoho: dict[str, Zoho] = {}


def zoho(box: dict) -> Zoho:
    if box["email"] not in _zoho:
        _zoho[box["email"]] = Zoho(box)
    return _zoho[box["email"]]


def ready(box: dict) -> None:
    """Everything a send needs before the email itself goes out (Zoho token, account id). Called before a
    message is marked 'sending', so a login problem leaves it simply waiting instead of 'maybe sent'."""
    if box.get("transport") == "zoho_api":
        zoho(box).account_id()


def not_sent(e: BaseException) -> bool:
    """True when the provider certainly did NOT accept the email, so it's safe to try again later.
    Timeouts and dropped connections after the request went out stay uncertain."""
    if isinstance(e, ZohoError):  # Zoho answered "no" (4xx): nothing was sent
        return e.http_status is not None and 400 <= e.http_status < 500
    if isinstance(e, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                      smtplib.SMTPAuthenticationError, smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError,
                      smtplib.SMTPConnectError)):
        return True
    return isinstance(e, (requests.ConnectTimeout, ConnectionRefusedError))


def send(box: dict, to: str, subject: str, body: str, thread: dict | None,
         custom_msg_id: str | None = None) -> tuple[str, str]:
    """Returns (message_id, provider_id)."""
    if box.get("transport") == "zoho_api":
        return zoho(box).send(to, subject, body, thread)
    return smtp_send(box, to, subject, body, thread, custom_msg_id=custom_msg_id)


def fetch(box: dict, days: int, known: set | None = None) -> list[Incoming]:
    """`known`: provider ids already stored, so their bodies aren't downloaded again."""
    if box.get("transport") == "zoho_api":
        return zoho(box).fetch(days, known)
    if not box.get("imap_host") or not box.get("password"):
        raise InboxUnavailable(f"{box['email']}: IMAP is not configured")
    return imap_fetch(box, days)


def save_draft(box: dict, to: str, subject: str, in_reply_to: str, body: str) -> bool:
    """Drafts land in the mailbox for IMAP inboxes; Zoho API users send with `outreach reply`."""
    if box.get("transport") == "zoho_api" or not box.get("imap_host"):
        return False
    imap_save_draft(box, to, subject, in_reply_to, body)
    return True


SEND_ERRORS = (smtplib.SMTPException, OSError, ZohoError, requests.RequestException)
