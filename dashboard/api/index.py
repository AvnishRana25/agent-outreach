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
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

SESSION_SECONDS = 14 * 24 * 3600
FAMILIES = {"review": {"approve", "reject", "regenerate"}, "reply": {"reply_send", "reply_done"},
            "post": {"post_done"}}
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
                              "body": str(e.get("body", ""))[:20_000]} for e in edits]}
    elif kind == "reply_send":
        text = str(payload.get("body", "")).strip()
        if not text:
            raise ValueError("empty reply")
        payload = {"body": text[:20_000]}
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
        ("SELECT id, kind, target, status, result, created_at, applied_at FROM actions ORDER BY id DESC LIMIT 80", ()),
    ])
    out = {"review": [], "reply": [], "post": []}
    for row in items:
        out.setdefault(row["kind"], []).append(json.loads(row["data"]))
    out["review"].sort(key=lambda x: -(x.get("confidence") or 0))
    out["reply"].sort(key=lambda x: x.get("received_at") or "")
    out.update({m["key"]: json.loads(m["value"]) for m in meta})
    out["actions"] = actions
    return out


# --------------------------------------------------------------------------- HTTP
class handler(BaseHTTPRequestHandler):
    def _route(self) -> str:
        u = urlparse(self.path)
        route = u.path.rstrip("/").rsplit("/", 1)[-1]
        if route == "index":  # reached through the vercel.json rewrite
            route = parse_qs(u.query).get("r", [""])[0]
        return route

    def _send(self, code: int, obj, cookie: str | None = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
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
            if route == "me":
                return self._send(200, {"authed": self._authed()})
            if not self._authed():
                return self._send(401, {"error": "login required"})
            if route == "data":
                return self._send(200, load_data())
            return self._send(404, {"error": "not found"})
        except (urllib.error.URLError, RuntimeError, KeyError) as e:
            return self._send(502, {"error": f"backend unavailable: {e}"})

    def do_POST(self):
        try:
            route = self._route()
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
            return self._send(404, {"error": "not found"})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e) or "bad request"})
        except (urllib.error.URLError, RuntimeError) as e:
            return self._send(502, {"error": f"backend unavailable: {e}"})

    def log_message(self, *args):
        pass
