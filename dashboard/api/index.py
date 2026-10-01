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
            "sync": {"sync"}}
STAGES = {"", "call_booked", "proposal_sent", "won", "lost"}
CURRENCIES = {"USD", "GBP", "AED", "INR"}
KIND_FAMILY = {k: fam for fam, kinds in FAMILIES.items() for k in kinds}
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
         "ORDER BY id DESC LIMIT 80", ()),
    ])
    out = {"review": [], "reply": [], "post": []}
    for row in items:
        out.setdefault(row["kind"], []).append(json.loads(row["data"]))
    out["review"].sort(key=lambda x: -(x.get("confidence") or 0))
    out["reply"].sort(key=lambda x: x.get("received_at") or "")
    out.update({m["key"]: json.loads(m["value"]) for m in meta})
    out["actions"] = actions
    return out


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
        writer.writerow(p)
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
                            from outreach.prospecting.config import daily_verified_target, provider_monthly_limit
                            stats["target_daily"] = daily_verified_target()
                            stats["provider_limits"] = {
                                "prospeo": provider_monthly_limit("prospeo"),
                                "hunter": provider_monthly_limit("hunter"),
                                "skrapp": provider_monthly_limit("skrapp"),
                            }
                        except Exception:
                            stats["target_daily"] = 38
                            stats["provider_limits"] = {}
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
                pw = str(self._json_body().get("password", ""))
                expected = os.environ.get("DASHBOARD_PASSWORD", "")
                if len(expected) < 10 or not hmac.compare_digest(pw.encode(), expected.encode()):
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

            # Prospecting POST routes
            if p_route == "prospects/enrich":
                body = self._json_body()
                from outreach.prospecting.pipeline.processor import enrich_prospect
                from outreach.prospecting.models import ProspectInput
                input_data = ProspectInput(
                    first_name=str(body.get("first_name", "")).strip(),
                    last_name=str(body.get("last_name", "")).strip(),
                    company=str(body.get("company", "")).strip(),
                    domain=str(body.get("domain", "")).strip() or None,
                    title=str(body.get("title", "")).strip() or None,
                    linkedin_url=str(body.get("linkedin_url", "")).strip() or None,
                )
                if not (input_data.first_name or input_data.last_name) or not (input_data.company or input_data.domain):
                    return self._send(400, {"error": "Name and Company or Domain are required"})
                result = enrich_prospect(input_data)
                return self._send(200, result.model_dump())

            if p_route == "prospects/bulk-enrich":
                body = self._json_body()
                from outreach.prospecting.pipeline.bulk import BulkProcessor
                from outreach.prospecting.models import ProspectInput
                items_raw = body.get("prospects", [])
                if not isinstance(items_raw, list):
                    return self._send(400, {"error": "prospects must be an array"})
                inputs = []
                for item in items_raw:
                    if isinstance(item, dict):
                        first = str(item.get("first_name", "")).strip()
                        last = str(item.get("last_name", "")).strip()
                        comp = str(item.get("company", "")).strip()
                        dom = str(item.get("domain", "")).strip() or None
                        if (first or last) and (comp or dom):
                            inputs.append(ProspectInput(
                                first_name=first,
                                last_name=last,
                                company=comp,
                                domain=dom,
                                title=str(item.get("title", "")).strip() or None,
                                linkedin_url=str(item.get("linkedin_url", "")).strip() or None,
                            ))
                target = int(body.get("max_verified_target", 38))
                processor = BulkProcessor(target_verified=target)
                results = processor.process_batch(inputs)
                verified = sum(1 for r in results if r.confidence_level in ("verified", "high_confidence") and r.final_email)
                return self._send(200, {
                    "total": len(inputs),
                    "processed": len(results),
                    "verified": verified,
                    "results": [r.model_dump() for r in results]
                })

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
