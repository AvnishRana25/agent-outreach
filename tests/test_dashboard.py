"""End-to-end dashboard test with a stand-in Turso server (real SQLite behind Turso's HTTP protocol):
laptop push -> browser login + actions through the Vercel handler -> laptop applies them."""
import http.client
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _cell(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": v}


def _py(a):
    t = a["type"]
    return None if t == "null" else int(a["value"]) if t == "integer" else float(a["value"]) if t == "float" else a["value"]


def fake_turso(path):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v2/pipeline" and self.headers["Authorization"] == "Bearer tok"
            reqs = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["requests"]
            conn = sqlite3.connect(path, isolation_level=None)
            results = []
            for r in reqs:
                if r["type"] == "close":
                    results.append({"type": "ok", "response": {"type": "close"}})
                    continue
                try:
                    cur = conn.execute(r["stmt"]["sql"], [_py(a) for a in r["stmt"].get("args", [])])
                    cols = [{"name": d[0]} for d in cur.description or []]
                    rows = [[_cell(v) for v in row] for row in cur.fetchall()]
                    results.append({"type": "ok", "response": {"type": "execute", "result": {
                        "cols": cols, "rows": rows, "affected_row_count": cur.rowcount}}})
                except sqlite3.Error as e:
                    results.append({"type": "error", "error": {"message": str(e)}})
            if conn.in_transaction:  # stream closed mid-transaction -> rolled back, like Turso
                conn.rollback()
            conn.close()
            body = json.dumps({"results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "local.db"))
    turso_srv = fake_turso(str(tmp_path / "remote.db"))
    monkeypatch.setenv("TURSO_DATABASE_URL", f"http://127.0.0.1:{turso_srv.server_port}")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse battery")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    from outreach import dashboard_local, db
    db.init()
    web = dashboard_local.server(port=0)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    yield web.server_port
    web.shutdown()
    turso_srv.shutdown()


def call(port, method, path, body=None, cookie="", header=True):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json", "Host": "localhost"}
    if header:
        headers["X-Requested-With"] = "dashboard"
    if cookie:
        headers["Cookie"] = cookie
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    data = r.read()
    try:
        data = json.loads(data)
    except ValueError:
        data = data.decode()
    return r.status, data, r.getheader("Set-Cookie") or ""


def seed():
    from outreach import db, personalize, research
    with db.connect() as conn:
        db.add_lead(conn, email="omar@palmrealty.ae", domain="palmrealty.ae", company="Palm Realty",
                    first_name="Omar", segment="gulf_realestate", status="verified", email_status="valid")
        db.add_lead(conn, email="sam@acme.io", domain="acme.io", company="Acme", first_name="Sam",
                    segment="uk_agencies", status="replied", email_status="valid")
    research.run(10, use_mock=True)
    assert personalize.run(10, use_mock=True) == 1
    with db.connect() as conn:  # stand-in for real Gemini output: mock text is purged by design
        omar_id = conn.execute("SELECT id FROM leads WHERE email='omar@palmrealty.ae'").fetchone()[0]
        conn.execute("UPDATE messages SET body='Hi Omar, a real draft about your WhatsApp leads.', review_note='' WHERE lead_id=?", (omar_id,))
        conn.execute("UPDATE leads SET research=? WHERE id=?",
                     ('{"company_summary": "Dubai brokerage", "best_hook": "WhatsApp ads", "fit_reason": "clear gap"}', omar_id))
        sam = conn.execute("SELECT id FROM leads WHERE email='sam@acme.io'").fetchone()[0]
        conn.execute("INSERT INTO replies (lead_id, inbox, imap_uid, message_id, from_addr, subject, body, received_at,"
                     " category, summary, suggested_reply) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sam, "workwithavnish@zohomail.in", "z9", "<r1@x>", "sam@acme.io", "Re: quick idea",
                      "Sounds good, what would it cost?", "2026-10-06T09:00:00+00:00", "question",
                      "Asks for a price", "Hi Sam, a fixed $180 for the first flow."))
        conn.execute("INSERT INTO posts (source, ext_id, title, url, relevant, draft_reply, status, found_at)"
                     " VALUES ('r/forhire','p1','[Hiring] n8n bot','https://reddit.com/p1',1,'I can build it','notified','x')")


def test_dashboard_round_trip(env, monkeypatch):
    port = env
    from outreach import dashboard_sync, db, transport
    seed()
    dashboard_sync.sync()

    # auth: no session, wrong password, missing CSRF header, then success
    assert call(port, "GET", "/api/data")[0] == 401
    assert call(port, "GET", "/api/me")[1] == {"authed": False}
    assert call(port, "POST", "/api/login", {"password": "nope"})[0] == 401
    assert call(port, "POST", "/api/login", {"password": "correct horse battery"}, header=False)[0] == 403
    status, _, set_cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})
    assert status == 200 and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    cookie = set_cookie.split(";")[0]
    assert call(port, "GET", "/api/data", cookie=cookie + "x")[0] == 401     # tampered session

    status, data, _ = call(port, "GET", "/api/data", cookie=cookie)
    assert status == 200 and data["synced_at"]
    [lead] = data["review"]
    assert lead["company"] == "Palm Realty" and len(lead["messages"]) >= 2
    assert data["reply"][0]["suggested_reply"].startswith("Hi Sam")
    assert data["post"][0]["title"] == "[Hiring] n8n bot"
    assert data["health"]["inboxes"][0]["email"] == "workwithavnish@zohomail.in"
    assert any(r["name"] == "ALL" for r in data["stats"]["segments"])

    first = lead["messages"][0]
    assert call(port, "POST", "/api/action", {"kind": "delete_all", "target": 1}, cookie=cookie)[0] == 400
    # change of mind before sync: reject, then approve replaces it
    assert call(port, "POST", "/api/action", {"kind": "reject", "target": lead["id"]}, cookie=cookie)[0] == 200
    assert call(port, "POST", "/api/action", {"kind": "approve", "target": lead["id"], "payload": {"edits": [
        {"id": first["id"], "subject": "a better subject", "body": "Edited body, Omar."}]}}, cookie=cookie)[0] == 200
    reply_id, post_id = data["reply"][0]["id"], data["post"][0]["id"]
    assert call(port, "POST", "/api/action", {"kind": "reply_send", "target": reply_id,
                                              "payload": {"body": "Hi Sam, $180 fixed. Start Monday?"}}, cookie=cookie)[0] == 200
    assert call(port, "POST", "/api/action", {"kind": "post_done", "target": post_id}, cookie=cookie)[0] == 200
    pending = [a for a in call(port, "GET", "/api/data", cookie=cookie)[1]["actions"] if a["status"] == "pending"]
    assert sorted(a["kind"] for a in pending) == ["approve", "post_done", "reply_send"]

    sent = []
    monkeypatch.setattr(transport, "send", lambda box, to, subject, body, thread: sent.append((to, subject, body, thread)) or ("<m>", "z"))
    dashboard_sync.sync()

    assert sent == [("sam@acme.io", "Re: quick idea", "Hi Sam, $180 fixed. Start Monday?",
                     {"message_id": "<r1@x>", "provider_id": "z9"})]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM leads WHERE email='omar@palmrealty.ae'").fetchone()[0] == "approved"
        m = conn.execute("SELECT subject, body, status FROM messages WHERE id=?", (first["id"],)).fetchone()
        assert tuple(m) == ("a better subject", "Edited body, Omar.", "approved")
        assert conn.execute("SELECT subject FROM messages WHERE lead_id=? AND step=1", (lead["id"],)).fetchone()[0] == \
            "Re: a better subject"
        assert conn.execute("SELECT handled FROM replies WHERE id=?", (reply_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()[0] == "done"

    data = call(port, "GET", "/api/data", cookie=cookie)[1]
    assert data["review"] == [] and data["reply"] == [] and data["post"] == []
    assert {a["status"] for a in data["actions"]} == {"applied"}


def test_bad_edit_is_reported_not_applied(env):
    port = env
    from outreach import dashboard_sync, db
    seed()
    dashboard_sync.sync()
    cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})[2].split(";")[0]
    lead = call(port, "GET", "/api/data", cookie=cookie)[1]["review"][0]
    call(port, "POST", "/api/action", {"kind": "approve", "target": lead["id"],
                                       "payload": {"edits": [{"id": 99999, "body": "hijack"}]}}, cookie=cookie)
    dashboard_sync.sync()
    [a] = call(port, "GET", "/api/data", cookie=cookie)[1]["actions"]
    assert a["status"] == "error" and "not part of lead" in a["result"]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id=?", (lead["id"],)).fetchone()[0] == "drafted"


def test_empty_edit_and_no_ai_instruction_cannot_be_approved(env):
    port = env
    from outreach import dashboard_sync, db
    seed()
    dashboard_sync.sync()
    cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})[2].split(";")[0]
    lead = call(port, "GET", "/api/data", cookie=cookie)[1]["review"][0]
    mid = lead["messages"][0]["id"]
    assert call(port, "POST", "/api/action", {"kind": "approve", "target": lead["id"],
        "payload": {"edits": [{"id": mid, "body": "  "}]}}, cookie=cookie)[0] == 400
    with db.connect() as conn:
        conn.execute("UPDATE leads SET site_text=? WHERE id=?",
                     ("AI-generated applications will not be reviewed.", lead["id"]))
    assert call(port, "POST", "/api/action", {"kind": "approve", "target": lead["id"]}, cookie=cookie)[0] == 200
    dashboard_sync.sync()
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM leads WHERE id=?", (lead["id"],)).fetchone()[0] == "drafted"
    action = call(port, "GET", "/api/data", cookie=cookie)[1]["actions"][0]
    assert action["status"] == "error" and "AI-generated" in action["result"]


def test_undo_cancels_pending(env):
    port = env
    from outreach import dashboard_sync
    seed()
    dashboard_sync.sync()
    cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})[2].split(";")[0]
    lead = call(port, "GET", "/api/data", cookie=cookie)[1]["review"][0]
    call(port, "POST", "/api/action", {"kind": "approve", "target": lead["id"]}, cookie=cookie)
    call(port, "POST", "/api/action", {"kind": "cancel", "target": lead["id"], "payload": {"family": "review"}},
         cookie=cookie)
    assert call(port, "GET", "/api/data", cookie=cookie)[1]["actions"] == []


def test_page_served(env):
    status, page, _ = call(env, "GET", "/")
    assert status == 200 and "<title>Outreach Desk</title>" in page


def test_port_in_use_gives_advice(env):
    from outreach import dashboard_local
    with pytest.raises(SystemExit) as e:
        dashboard_local.serve(env)            # the fixture's server already holds this port
    assert "already in use" in str(e.value) and f"--port {env + 1}" in str(e.value)


def test_engine_controls_round_trip(env, monkeypatch):
    port = env
    from outreach import dashboard_sync, db, engine
    seed()
    spawned = []
    monkeypatch.setattr(engine, "spawn", lambda job: spawned.append(job) or "started")
    dashboard_sync.sync()
    cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})[2].split(";")[0]
    data = call(port, "GET", "/api/data", cookie=cookie)[1]
    assert data["engine"]["sending_paused"] is False and len(data["engine"]["adlib"]) >= 1
    assert "gulf_realestate" in data["engine"]["segments"]

    assert call(port, "POST", "/api/action", {"kind": "add_lead", "target": 77, "payload": {"company": "X"}},
                cookie=cookie)[0] == 400                       # needs a website or email
    for body in ({"kind": "run_prepare", "target": 1}, {"kind": "pause_sending", "target": 1},
                 {"kind": "add_lead", "target": 77, "payload": {"company": "Palm Homes", "website": "palmhomes.ae",
                                                                 "segment": "gulf_realestate", "notes": "WhatsApp ad for 1BR"}},
                 {"kind": "add_lead", "target": 78, "payload": {"company": "Bad", "website": "bad.ae", "segment": "nope"}}):
        assert call(port, "POST", "/api/action", body, cookie=cookie)[0] == 200
    dashboard_sync.sync()
    assert spawned == ["prepare"]
    with db.connect() as conn:
        assert db.get_state(conn, "sending_paused") == "1"
        lead = conn.execute("SELECT * FROM leads WHERE domain='palmhomes.ae'").fetchone()
        assert lead["source"] == "adlibrary" and lead["website"] == "https://palmhomes.ae"
        assert "WhatsApp ad for 1BR" in lead["source_text"]
    acts = {a["kind"] + str(a["target"]): a for a in call(port, "GET", "/api/data", cookie=cookie)[1]["actions"]}
    assert acts["add_lead78"]["status"] == "error" and "unknown segment" in acts["add_lead78"]["result"]
    assert call(port, "GET", "/api/data", cookie=cookie)[1]["engine"]["sending_paused"] is True


def test_deal_stage_and_plan_from_dashboard(env, monkeypatch):
    port = env
    from outreach import dashboard_sync, db, growth
    seed()
    monkeypatch.setattr(growth, "make_plan", lambda rid: "plan ready")
    dashboard_sync.sync()
    cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})[2].split(";")[0]
    data = call(port, "GET", "/api/data", cookie=cookie)[1]
    [deal] = data["pipeline"]                               # Sam's positive reply
    reply = data["reply"][0]
    assert call(port, "POST", "/api/action", {"kind": "set_stage", "target": deal["id"],
                                              "payload": {"stage": "nope"}}, cookie=cookie)[0] == 400
    for body in ({"kind": "set_stage", "target": deal["id"], "payload": {"stage": "proposal_sent", "value": "350",
                                                                           "currency": "GBP", "note": "sent Tue",
                                                                           "next_action": "Call founder", "next_due": "2026-10-09"}},
                 {"kind": "make_plan", "target": reply["id"]}):
        assert call(port, "POST", "/api/action", body, cookie=cookie)[0] == 200
    dashboard_sync.sync()
    with db.connect() as conn:
        row = conn.execute("SELECT deal_stage, deal_value, deal_note, deal_currency, deal_next_action, deal_next_due "
                           "FROM leads WHERE id=?", (deal["id"],)).fetchone()
    assert tuple(row) == ("proposal_sent", 350.0, "sent Tue", "GBP", "Call founder", "2026-10-09")
    acts = call(port, "GET", "/api/data", cookie=cookie)[1]["actions"]
    assert {a["kind"]: a["result"] for a in acts}["make_plan"] == "plan ready"


def test_dashboard_reloads_api_after_pull(tmp_path, monkeypatch):
    import os
    import shutil
    import time
    from outreach import config, dashboard_local
    dash = tmp_path / "dashboard"
    shutil.copytree(config.ROOT / "dashboard", dash)
    monkeypatch.setattr(dashboard_local, "DASH_DIR", dash)
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    srv = dashboard_local.server(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert call(srv.server_port, "GET", "/api/version")[0] != 200     # route not there yet
        api = dash / "api" / "index.py"
        api.write_text(api.read_text().replace('            if route == "me":',
                                               '            if route == "version":\n'
                                               '                return self._send(200, {"v": 2})\n'
                                               '            if route == "me":'))
        os.utime(api, (time.time() + 5, time.time() + 5))   # a pull always changes the file time
        assert call(srv.server_port, "GET", "/api/version")[1] == {"v": 2}
        api.write_text("this is not python (")                # a half-written file keeps the old code
        os.utime(api, (time.time() + 10, time.time() + 10))
        assert call(srv.server_port, "GET", "/api/version")[1] == {"v": 2}
    finally:
        srv.shutdown()


def _queue(port, cookie, kind, target, payload=None):
    s, d, _ = call(port, "POST", "/api/action", {"kind": kind, "target": target, "payload": payload or {}}, cookie)
    assert s == 200, d


def _login(port):
    s, _, set_cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})
    assert s == 200
    return set_cookie.split(";")[0]


def test_gemini_actions_go_to_the_assist_job(env, monkeypatch):
    from outreach import dashboard_sync, engine, growth, turso
    dashboard_sync.init_remote()
    cookie = _login(env)
    _queue(env, cookie, "make_plan", 5)
    _queue(env, cookie, "set_stage", 6, {"stage": "won"})
    spawned, applied = [], []
    monkeypatch.setattr(engine, "spawn", lambda job: spawned.append(job) or "started")
    monkeypatch.setattr(growth, "make_plan", lambda rid: applied.append(("plan", rid)) or "plan ready")
    monkeypatch.setattr(growth, "set_stage", lambda *a: applied.append(("stage", a[0])) or "stage: Won")
    assert dashboard_sync.pull_safe() == 1                  # the tick: quick ones only
    assert applied == [("stage", 6)] and spawned == ["assist"]
    assert dashboard_sync.pull(slow=True) == 1              # the assist job
    assert applied[-1] == ("plan", 5)
    [rows] = turso.run(["SELECT COUNT(*) AS n FROM actions WHERE status='pending'"])
    assert int(rows[0]["n"]) == 0


def test_sync_now_starts_an_engine_run(env, monkeypatch):
    from datetime import datetime, timezone
    from outreach import dashboard_local, dashboard_sync, db, engine
    dashboard_sync.init_remote()
    with db.connect() as conn:
        db.set_state(conn, "engine:heartbeat", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    kicks = []
    monkeypatch.setattr(engine, "kick_tick", lambda: kicks.append(1) or "started")
    state = {}
    assert dashboard_local.watch_once(state) == "idle"      # nothing to do: no run
    _queue(env, _login(env), "sync", 1)
    assert dashboard_local.watch_once(state) == "dashboard action: started"
    assert dashboard_local.watch_once(state) == "idle"      # don't start a run every 20 seconds
    assert dashboard_sync.pull_safe() == 1                  # the run applies it
    state.clear()
    assert dashboard_local.watch_once(state) == "idle"


def test_watchdog_restarts_a_stopped_engine(env, monkeypatch):
    from outreach import dashboard_local, dashboard_sync, db, engine
    dashboard_sync.init_remote()
    with db.connect() as conn:
        db.set_state(conn, "engine:heartbeat", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(engine, "kick_tick", lambda: "started")
    assert dashboard_local.watch_once({}) == "engine overdue: started"


def test_settings_error_shows_until_a_good_run(env):
    from outreach import dashboard_sync, engine
    dashboard_sync.init_remote()
    engine.report_config_error("config/settings.yaml has a formatting mistake near line 257")
    cookie = _login(env)
    _, data, _ = call(env, "GET", "/api/data", cookie=cookie)
    assert "line 257" in data["engine_error"]["text"]
    dashboard_sync.push()
    _, data, _ = call(env, "GET", "/api/data", cookie=cookie)
    assert "engine_error" not in data


def test_run_all_queues_and_starts_three_jobs(env, monkeypatch):
    port = env
    from outreach import dashboard_sync, engine
    started = []
    monkeypatch.setattr(engine, "spawn", lambda job: started.append(job) or "started")
    dashboard_sync.sync()
    cookie = call(port, "POST", "/api/login", {"password": "correct horse battery"})[2].split(";")[0]
    assert call(port, "POST", "/api/action", {"kind": "run_all", "target": 1}, cookie=cookie)[0] == 200
    queued = call(port, "GET", "/api/data", cookie=cookie)[1]["actions"]
    assert queued[0]["kind"] == "run_all" and queued[0]["status"] == "pending"
    dashboard_sync.sync()
    assert started == ["prepare", "community", "content"]
    applied = call(port, "GET", "/api/data", cookie=cookie)[1]["actions"]
    assert applied[0]["status"] == "applied"


def test_run_all_starts_remaining_jobs_after_one_fails(monkeypatch):
    from outreach import dashboard_sync, engine
    started = []

    def spawn(job):
        started.append(job)
        if job == "community":
            raise RuntimeError("could not start")
        return "started"

    monkeypatch.setattr(engine, "spawn", spawn)
    assert dashboard_sync._run_all(1, {}).startswith("error:")
    assert started == ["prepare", "community", "content"]


def test_login_lockout_after_ten_wrong_passwords(env, monkeypatch):
    from outreach import dashboard_sync
    api = __import__("outreach.dashboard_local", fromlist=["api_module"]).api_module()
    dashboard_sync.init_remote()
    monkeypatch.setattr("time.sleep", lambda s: None)
    for _ in range(10):
        assert call(env, "POST", "/api/login", {"password": "nope"})[0] == 401
    s, d, _ = call(env, "POST", "/api/login", {"password": "correct horse battery"})
    assert s == 429 and "15 minutes" in d["error"]
    assert api.LOCK_AFTER == 10


def test_prospect_searches_are_queued_and_csv_is_safe(env, monkeypatch):
    from outreach import dashboard_sync, db, turso
    dashboard_sync.init_remote()
    cookie = _login(env)
    rows = [{"first_name": "Ana", "last_name": "Ruiz", "company": "Acme"}, {"first_name": "", "company": "x"}] * 3
    s, d, _ = call(env, "POST", "/api/prospects/bulk-enrich", {"prospects": rows}, cookie)
    assert s == 202 and d == {"queued": True, "count": 3, "skipped": 3}
    s, d, _ = call(env, "POST", "/api/prospects/bulk-enrich", {"prospects": rows[:1]}, cookie)
    [pending] = turso.run(["SELECT kind, payload FROM actions WHERE status='pending'"])
    assert [p["kind"] for p in pending] == ["find_prospects", "find_prospects"]   # the second didn't replace the first
    s, d, _ = call(env, "GET", "/api/prospects?limit=abc", cookie=cookie)
    assert s == 400
    with db.connect() as conn:
        db.add_prospect(conn, full_name="=HYPERLINK(\"http://evil\")", company="Acme", final_email="a@acme.com")
    s, csv_text, _ = call(env, "GET", "/api/prospects/export", cookie=cookie)
    assert "'=HYPERLINK" in csv_text
    s, stats, _ = call(env, "GET", "/api/prospects/stats", cookie=cookie)
    assert stats["budget"]["hunter"]["usable_monthly"] == 20


def test_queued_search_runs_in_the_background_job(env, monkeypatch):
    from outreach import dashboard_sync
    from outreach.prospecting.pipeline import bulk
    seen = []
    monkeypatch.setattr(bulk.BulkProcessor, "process_batch", lambda self, people: seen.extend(people) or [])
    out = dashboard_sync._find_prospects(1, {"prospects": [{"first_name": "Ana", "last_name": "Ruiz", "company": "Acme",
                                                            "domain": "", "title": "", "linkedin_url": ""}]})
    assert out.startswith("searched 0 of 1") and seen[0].domain is None and seen[0].first_name == "Ana"
    assert "find_prospects" in dashboard_sync.SLOW


def test_clear_activity_keeps_waiting_items(env):
    from outreach import dashboard_sync, turso
    dashboard_sync.init_remote()
    cookie = _login(env)
    _queue(env, cookie, "approve", 5)
    _queue(env, cookie, "approve_many", 77, {"ids": [1, 2]})
    turso.run(["UPDATE actions SET status='applied' WHERE kind='approve'"])
    s, d, _ = call(env, "POST", "/api/activity/clear", {}, cookie)
    assert s == 200 and d["left"] == 1
    [rows] = turso.run(["SELECT kind FROM actions"])
    assert [r["kind"] for r in rows] == ["approve_many"]
    s, _, _ = call(env, "POST", "/api/action", {"kind": "approve_many", "target": 3, "payload": {"ids": ["x"]}}, cookie)
    assert s == 400
