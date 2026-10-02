"""Dashboard API (Vercel Python function; standard library only).

Routes (all JSON):
  POST /api/login    {password}           -> sets a signed, HttpOnly session cookie
  POST /api/logout
  GET  /api/data                          -> snapshot pushed by the laptop + recent actions
  POST /api/action   {kind, target, payload}  -> queued; the laptop applies it on its next sync

Environment (Vercel -> Project -> Settings -> Environment Variables):
  DASHBOARD_PASSWORD, SESSION_SECRET, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
The API never sends email or calls Gemini itself; it only records what you decided.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

SESSION_SECONDS = 14 * 24 * 3600
FAMILIES = {"review": {"approve", "reject", "regenerate"}, "reply": {"reply_send", "reply_done"},
            "post": {"post_done"}, "run": {"run_prepare"}, "community": {"run_community"},
            "sending": {"pause_sending", "resume_sending"}, "lead": {"add_lead"},
            "deal": {"set_stage"}, "plan": {"make_plan"}, "content": {"run_content"},
            "sync": {"sync"}, "held": {"send_anyway", "mark_sent", "retry_send"},
            "prospect": {"promote_prospect"}, "find": {"find_prospects"},
            "bulk": {"approve_many"}, "runall": {"run_all"}, "draftlead": {"draft_lead"},
            "outcome": {"record_outcome"}, "tick": {"trigger_tick"}}
STAGES = {"", "call_booked", "proposal_sent", "won", "lost"}
OUTCOMES = {"delivered", "bounced", "replied", "positive_reply", "negative_reply",
            "meeting", "interview", "project_discussion", "offer", "won_project", "lost_project", "no_response"}
CURRENCIES = {"USD", "GBP", "AED", "INR"}
KIND_FAMILY = {k: fam for fam, kinds in FAMILIES.items() for k in kinds}
MAX_BULK = 200   # prospects per bulk request; bigger files are split by the page
MAX_BODY = 100_000


# --------------------------------------------------------------------------- Turso over HTTP
def _arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int) and not isinstance(v, bool):
        return {"type": "integer", "value": str(v)}
    return {"type": "text", "value": str(v)}


def _val(cell):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    return cell.get("value")


def turso(statements):
    url = os.environ["TURSO_DATABASE_URL"].replace("libsql://", "https://", 1).rstrip("/")
    reqs = [{"type": "execute", "stmt": {"sql": s, "args": [_arg(a) for a in args]}} for s, args in statements]
    body = json.dumps({"requests": reqs + [{"type": "close"}]}).encode()
    req = urllib.request.Request(f"{url}/v2/pipeline", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('TURSO_AUTH_TOKEN', '')}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        results = json.loads(resp.read())["results"][:-1]
    out = []
    for res in results:
        if res.get("type") != "ok":
            raise RuntimeError(res.get("error", {}).get("message", "turso error"))
        r = res["response"]["result"]
        cols = [c["name"] for c in r.get("cols", [])]
        out.append([dict(zip(cols, map(_val, row))) for row in r.get("rows", [])])
    return out


# --------------------------------------------------------------------------- sessions
def _secret() -> bytes:
    s = os.environ.get("SESSION_SECRET", "")
    if len(s) < 16:
        raise RuntimeError("SESSION_SECRET must be set (16+ random characters)")
    return s.encode()


def make_session(now: float | None = None) -> str:
    exp = str(int((now or time.time()) + SESSION_SECONDS))
    return exp + "." + hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()


def valid_session(token: str, now: float | None = None) -> bool:
    exp, _, sig = (token or "").partition(".")
    if not exp.isdigit() or int(exp) < (now or time.time()):
        return False
    good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


# --------------------------------------------------------------------------- validation
def clean_action(body: dict) -> tuple[str, int, dict]:
    kind = body.get("kind")
    if kind not in KIND_FAMILY and kind != "cancel":
        raise ValueError("unknown action")
    target = body.get("target")
    if not isinstance(target, int) or target <= 0:
        raise ValueError("bad target")
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("bad payload")
    if kind == "approve":
        edits = payload.get("edits") or []
        if not isinstance(edits, list) or len(edits) > 10:
            raise ValueError("bad edits")
        payload = {"edits": [{"id": int(e["id"]), "subject": str(e.get("subject", ""))[:300],
                              "body": str(e.get("body", ""))[:20_000].strip()} for e in edits]}
        if any(not e["body"] for e in payload["edits"]):
            raise ValueError("email body cannot be empty")
    elif kind == "reply_send":
        text = str(payload.get("body", "")).strip()
        if not text:
            raise ValueError("empty reply")
        payload = {"body": text[:20_000]}
    elif kind == "approve_many":
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 300 or not all(isinstance(i, int) and i > 0 for i in ids):
            raise ValueError("bad ids")
        payload = {"ids": ids}
    elif kind == "find_prospects":
        people = payload.get("prospects")
        if not isinstance(people, list) or not people or len(people) > MAX_BULK:
            raise ValueError("bad prospects")
        cleaned = [clean_person(p) for p in people if isinstance(p, dict)]
        payload = {"prospects": [p for p in cleaned if p], "target": int(payload.get("target") or 38)}
    elif kind == "promote_prospect":
        segment = str(payload.get("segment", "")).strip()[:60]
        if not segment.replace("_", "").isalnum():
            raise ValueError("bad segment")
        payload = {"segment": segment}
    elif kind == "add_lead":
        fields = {"company": 120, "website": 300, "email": 200, "segment": 60, "country": 60, "notes": 1000,
                  "source": 60, "opportunity_type": 30}
        payload = {k: str(payload.get(k, "")).strip()[:n] for k, n in fields.items()}
        if not payload["company"] or not (payload["website"] or payload["email"]):
            raise ValueError("company plus a website or email are required")
        if not payload["segment"].replace("_", "").isalnum():
            raise ValueError("bad segment")
        if payload.get("opportunity_type") and payload["opportunity_type"] not in ("contract", "internship"):
            raise ValueError("bad opportunity_type")
    elif kind == "set_stage":
        stage = str(payload.get("stage", ""))
        if stage not in STAGES:
            raise ValueError("bad stage")
        value = payload.get("value")
        if value not in (None, ""):
            value = float(value)
            if not 0 <= value < 10_000_000:
                raise ValueError("bad value")
        currency = str(payload.get("currency") or "USD").upper()
        if currency not in CURRENCIES:
            raise ValueError("bad currency")
        opp_type = str(payload.get("opportunity_type", "")).strip().lower()
        if opp_type and opp_type not in ("contract", "internship"):
            raise ValueError("bad opportunity_type")
        next_due = str(payload.get("next_due") or "")
        if next_due:
            date.fromisoformat(next_due)
        payload = {"stage": stage, "value": value if value != "" else None, "currency": currency,
                   "note": str(payload.get("note", ""))[:500],
                   "next_action": str(payload.get("next_action", ""))[:300], "next_due": next_due,
                   "opportunity_type": opp_type}
    elif kind == "record_outcome":
        outcome = str(payload.get("outcome", "")).strip().lower()
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        notes = str(payload.get("notes", ""))[:500]
        stage = str(payload.get("stage", ""))
        if stage and stage not in STAGES:
            raise ValueError("bad stage")
        payload = {"outcome": outcome, "notes": notes, "stage": stage}
    elif kind == "trigger_tick":
        dry_run = bool(payload.get("dry_run", True))
        payload = {"dry_run": dry_run, "max_sends": int(payload.get("max_sends", 2))}
    elif kind == "cancel":
        fam = payload.get("family")
        if fam not in FAMILIES:
            raise ValueError("bad family")
        payload = {"family": fam}
    else:
        payload = {}
    return kind, target, payload


def queue_statements(kind: str, target: int, payload: dict) -> list:
    fam = payload["family"] if kind == "cancel" else KIND_FAMILY[kind]
    kinds = sorted(FAMILIES[fam])
    marks = ",".join("?" * len(kinds))
    # A newer decision on the same item replaces one the laptop hasn't applied yet.
    stmts = [(f"DELETE FROM actions WHERE status='pending' AND target=? AND kind IN ({marks})", (target, *kinds))]
    if kind != "cancel":
        stmts.append(("INSERT INTO actions (kind, target, payload, created_at) VALUES (?,?,?,?)",
                      (kind, target, json.dumps(payload), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))))
    return stmts


def load_data() -> dict:
    items, meta, actions = turso([
        ("SELECT kind, id, data FROM dash_items ORDER BY kind, sort DESC", ()),
        ("SELECT key, value FROM dash_meta", ()),
        ("SELECT id, kind, target, payload, status, result, created_at, applied_at FROM actions "
         "ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id DESC LIMIT 80", ()),
    ])
    out = {"review": [], "reply": [], "post": [], "held": []}
    for row in items:
        out.setdefault(row["kind"], []).append(json.loads(row["data"]))
    out["review"].sort(key=lambda x: -(x.get("confidence") or 0))
    out["reply"].sort(key=lambda x: x.get("received_at") or "")
    out.update({m["key"]: json.loads(m["value"]) for m in meta})
    out["actions"] = actions
    return out


def clean_person(raw: dict) -> dict | None:
    """One prospect from the dashboard, trimmed to sane lengths; None without a name and company/domain."""
    limits = {"first_name": 60, "last_name": 60, "company": 120, "domain": 120, "title": 120, "linkedin_url": 300}
    p = {k: str(raw.get(k) or "").strip()[:n] for k, n in limits.items()}
    if not (p["first_name"] or p["last_name"]) or not (p["company"] or p["domain"]):
        return None
    return p


def _csv_safe(value) -> str:
    """Spreadsheet apps run cells that start with = + - @ as formulas; names and titles come from the web."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# --------------------------------------------------------------------------- login throttle
LOCK_AFTER, LOCK_WINDOW = 10, 15 * 60   # 10 wrong passwords from one address in 15 minutes -> 15-minute lock


def _fail_key(ip: str) -> str:
    return "login_fail:" + hashlib.sha256(ip.encode()).hexdigest()[:16]


def login_locked(ip: str) -> bool:
    try:
        [rows] = turso([("SELECT value FROM dash_meta WHERE key=?", (_fail_key(ip),))])
    except Exception:   # the database being down must not lock you out
        return False
    if not rows:
        return False
    st = json.loads(rows[0]["value"])
    return st.get("n", 0) >= LOCK_AFTER and time.time() - st.get("since", 0) < LOCK_WINDOW


def login_failed(ip: str) -> None:
    try:
        [rows] = turso([("SELECT value FROM dash_meta WHERE key=?", (_fail_key(ip),))])
        st = json.loads(rows[0]["value"]) if rows else {}
        if time.time() - st.get("since", 0) >= LOCK_WINDOW:
            st = {"n": 0, "since": time.time()}
        st["n"] = st.get("n", 0) + 1
        turso([("INSERT OR REPLACE INTO dash_meta (key, value) VALUES (?,?)", (_fail_key(ip), json.dumps(st)))])
    except Exception:
        pass


def _job_id() -> int:
    """Each queued search gets its own target, so a second one doesn't replace the first while it waits."""
    return int(time.time() * 1000) % 2_000_000_000


def _get_db():
    try:
        from outreach import db
        return db
    except ImportError:
        return None


def _prospects_to_csv(prospects: list[dict]) -> str:
    import io, csv
    out = io.StringIO()
    fields = [
        "id", "full_name", "first_name", "last_name", "company", "title",
        "domain", "final_email", "confidence_score", "confidence_level",
        "email_status", "email_pattern", "source", "source_url",
        "mx_valid", "catch_all", "verification_provider", "linkedin_url"
    ]
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for p in prospects:
        writer.writerow({k: _csv_safe(v) for k, v in p.items()})
    return out.getvalue()


# --------------------------------------------------------------------------- HTTP
class handler(BaseHTTPRequestHandler):
    def _route(self) -> str:
        u = urlparse(self.path)
        route = u.path.rstrip("/").rsplit("/", 1)[-1]
        if route == "index":  # reached through the vercel.json rewrite
            route = parse_qs(u.query).get("r", [""])[0]
        return route

    def _parsed_route(self) -> tuple[str, dict]:
        u = urlparse(self.path)
        p = u.path.rstrip("/")
        if p.endswith("/index"):  # reached through the vercel.json rewrite
            p = "/" + parse_qs(u.query).get("r", [""])[0].strip("/")
        if p.startswith("/api/"):
            p = p[5:]
        elif p.startswith("/api"):
            p = p[4:]
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        return p.strip("/"), params

    def _send(self, code: int, obj, cookie: str | None = None) -> None:
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_csv(self, filename: str, content: str) -> None:
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, value: str, max_age: int) -> str:
        host = (self.headers.get("Host") or "").split(":")[0]
        secure = "" if host in ("localhost", "127.0.0.1") else "; Secure"
        return f"session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _client_ip(self) -> str:
        # Vercel puts the real client first in X-Forwarded-For; locally it's always 127.0.0.1
        fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return fwd or self.client_address[0]

    def _authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return "session" in c and valid_session(c["session"].value)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data

    def do_GET(self):
        try:
            route = self._route()
            p_route, params = self._parsed_route()
            if route == "me":
                return self._send(200, {"authed": self._authed()})
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if route == "data":
                return self._send(200, load_data())

            # Prospecting routes
            if p_route == "prospects/stats":
                db_mod = _get_db()
                if db_mod:
                    with db_mod.connect() as conn:
                        stats = db_mod.get_prospecting_stats(conn)
                        try:
                            from outreach.prospecting.config import daily_verified_target
                            from outreach.prospecting.providers import budget
                            stats["target_daily"] = daily_verified_target()
                            stats["budget"] = budget.summary()
                        except Exception:
                            stats["target_daily"] = 38
                            stats["budget"] = {}
                        return self._send(200, stats)
                # Fallback to Turso dash_meta
                try:
                    meta = turso([("SELECT value FROM dash_meta WHERE key='prospecting'", ())])
                    if meta and meta[0]:
                        return self._send(200, json.loads(meta[0][0]["value"]))
                except Exception:
                    pass
                return self._send(200, {"total": 0, "verified_today": 0, "target_daily": 38, "counts_by_status": {}, "credits_used": {}, "provider_limits": {}})

            if p_route == "prospects/export":
                db_mod = _get_db()
                prospects = []
                if db_mod:
                    with db_mod.connect() as conn:
                        prospects = db_mod.list_prospects(conn, limit=5000)
                csv_data = _prospects_to_csv(prospects)
                return self._send_csv("prospects.csv", csv_data)

            if p_route.startswith("prospects/") and p_route.split("/")[1].isdigit():
                pid = int(p_route.split("/")[1])
                db_mod = _get_db()
                if db_mod:
                    with db_mod.connect() as conn:
                        p = db_mod.get_prospect(conn, pid)
                        if p:
                            return self._send(200, p)
                return self._send(404, {"error": "prospect not found"})

            if p_route == "prospects":
                status = params.get("status")
                search = params.get("search")
                conf = params.get("confidence")
                limit = min(200, max(1, int(params.get("limit", 50))))
                offset = max(0, int(params.get("offset", 0)))
                db_mod = _get_db()
                if db_mod:
                    with db_mod.connect() as conn:
                        prospects = db_mod.list_prospects(
                            conn, status=status, confidence_level=conf, search=search, limit=limit, offset=offset
                        )
                        total = db_mod.count_prospects(conn, status=status)
                        return self._send(200, {"prospects": prospects, "total": total, "limit": limit, "offset": offset})
                # Fallback to Turso dash_meta recent prospects
                try:
                    meta = turso([("SELECT value FROM dash_meta WHERE key='prospecting'", ())])
                    if meta and meta[0]:
                        info = json.loads(meta[0][0]["value"])
                        recent = info.get("recent", [])
                        return self._send(200, {"prospects": recent[:limit], "total": info.get("total", len(recent)), "limit": limit, "offset": offset})
                except Exception:
                    pass
                return self._send(200, {"prospects": [], "total": 0, "limit": limit, "offset": offset})

            return self._send(404, {"error": "not found"})
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": str(e) or "bad request"})
        except (urllib.error.URLError, RuntimeError, KeyError) as e:
            return self._send(502, {"error": f"backend unavailable: {e}"})

    def do_POST(self):
        try:
            route = self._route()
            p_route, params = self._parsed_route()
            # Custom header: browsers can't send it cross-site without a CORS preflight we never allow.
            if self.headers.get("X-Requested-With") != "dashboard":
                return self._send(403, {"error": "forbidden"})
            if route == "login":
                ip = self._client_ip()
                if login_locked(ip):
                    return self._send(429, {"error": "too many wrong passwords; try again in 15 minutes"})
                pw = str(self._json_body().get("password", ""))
                expected = os.environ.get("DASHBOARD_PASSWORD", "")
                if len(expected) < 10 or not hmac.compare_digest(pw.encode(), expected.encode()):
                    login_failed(ip)
                    time.sleep(1.0)
                    return self._send(401, {"error": "wrong password"})
                return self._send(200, {"ok": True}, self._cookie(make_session(), SESSION_SECONDS))
            if route == "logout":
                return self._send(200, {"ok": True}, self._cookie("", 0))
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if route == "action":
                kind, target, payload = clean_action(self._json_body())
                turso(queue_statements(kind, target, payload))
                return self._send(200, {"ok": True})
            if p_route == "activity/clear":
                # Only finished entries: anything still waiting for the Mac must still run.
                [_, rows] = turso([("DELETE FROM actions WHERE status != 'pending'", ()),
                                   ("SELECT COUNT(*) AS n FROM actions", ())])
                return self._send(200, {"ok": True, "left": int(rows[0]["n"]) if rows else 0})

            # Prospecting POST routes
            if p_route == "prospects/enrich":
                person = clean_person(self._json_body())
                if not person:
                    return self._send(400, {"error": "Name and Company or Domain are required"})
                if _get_db() is None:   # on Vercel: the Mac does the search (it has the keys and the budget)
                    turso(queue_statements("find_prospects", _job_id(), {"prospects": [person]}))
                    return self._send(202, {"queued": True, "count": 1})
                from outreach.prospecting.pipeline.processor import enrich_prospect
                return self._send(200, enrich_prospect(person).model_dump())

            if p_route == "prospects/bulk-enrich":
                body = self._json_body()
                items = body.get("prospects", [])
                if not isinstance(items, list):
                    return self._send(400, {"error": "prospects must be an array"})
                people = [p for p in (clean_person(i) for i in items[:MAX_BULK] if isinstance(i, dict)) if p]
                if not people:
                    return self._send(400, {"error": "no valid rows (each needs a name and a company or domain)"})
                # Runs in the background on the Mac (minutes for a big file), not inside this request.
                turso(queue_statements("find_prospects", _job_id(), {"prospects": people,
                                                             "target": int(body.get("max_verified_target") or 38)}))
                return self._send(202, {"queued": True, "count": len(people), "skipped": len(items) - len(people)})

            if p_route == "prospects/export":
                body = self._json_body()
                status = body.get("status")
                search = body.get("search")
                db_mod = _get_db()
                prospects = []
                if db_mod:
                    with db_mod.connect() as conn:
                        prospects = db_mod.list_prospects(conn, status=status, search=search, limit=5000)
                csv_data = _prospects_to_csv(prospects)
                return self._send_csv("prospects.csv", csv_data)

            return self._send(404, {"error": "not found"})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e) or "bad request"})
        except (urllib.error.URLError, RuntimeError) as e:
            return self._send(502, {"error": f"backend unavailable: {e}"})

    def log_message(self, *args):
        pass
