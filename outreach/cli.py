"""Command line entry point: `python -m outreach <command>`."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import (community, config, dashboard_sync, db, engine, enrich, importer, personalize, prospect,
               prospecting, replies, report, research, review, sender, sources, transport, verify)


def main() -> None:
    ap = argparse.ArgumentParser(prog="outreach", description="Automated, personalised cold email pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database")

    p = sub.add_parser("prospect", help="find new leads from free public sources (settings.yaml -> prospecting)")
    p.add_argument("--source", choices=sorted(prospect.all_runners()), help="run only this source")

    p = sub.add_parser("import", help="import leads from a CSV")
    p.add_argument("csv", type=Path)
    p.add_argument("--segment", help="segment for rows without a segment column")
    p.add_argument("--source", default="csv")

    p = sub.add_parser("enrich", help="scrape lead websites for signals and published emails")
    p.add_argument("--limit", type=int, default=150)

    sub.add_parser("verify", help="check emails (syntax, MX, role) and score leads")

    p = sub.add_parser("research", help="Gemini company research + fit score")
    p.add_argument("--limit", type=int, default=45)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--segment", help="run research only on leads from this segment")

    p = sub.add_parser("draft", help="write sequences for today's best researched leads")
    p.add_argument("--limit", type=int, default=38)
    p.add_argument("--mock", action="store_true", help="placeholder drafts, no API calls")
    p.add_argument("--segment", help="draft sequences only for leads from this segment")

    p = sub.add_parser("prepare", help="daily job: prospect + enrich + verify + research + draft")
    p.add_argument("--limit", type=int, default=38)
    p.add_argument("--mock", action="store_true")
    p.add_argument("--skip-prospect", action="store_true")

    sub.add_parser("review", help="approve / edit / regenerate / reject today's drafts")

    p = sub.add_parser("approve", help="bulk-approve drafts at or above a confidence")
    p.add_argument("--min-confidence", type=float, default=0.85)

    p = sub.add_parser("community", help="check forums/Reddit for [Hiring] posts, draft replies, ping Telegram")
    p.add_argument("--mock", action="store_true")

    p = sub.add_parser("post-done", help="mark a community post as answered")
    p.add_argument("post_id", type=int)

    sub.add_parser("adlib", help="write today's Meta Ad Library searches to data/adlibrary_today.md")

    p = sub.add_parser("linkedin", help="write today's hand-sent LinkedIn tasks to data/linkedin_today.md")

    p = sub.add_parser("send", help="send due emails (run from cron every ~10 min)")
    p.add_argument("--max", type=int, default=2)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("sync", help="pull replies, stop sequences, draft answers")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--mock", action="store_true")

    p = sub.add_parser("reply", help="edit and send the drafted answer to reply #id")
    p.add_argument("reply_id", type=int)

    sub.add_parser("report", help="funnel numbers")

    p = sub.add_parser("done", help="mark a positive reply as handled")
    p.add_argument("reply_id", type=int)

    p = sub.add_parser("suppress", help="never email this address, or a whole @domain")
    p.add_argument("email")

    sub.add_parser("pause", help="pause automatic sending")
    sub.add_parser("resume", help="resume automatic sending")

    sub.add_parser("content", help="draft this week's three LinkedIn posts from your proof points")
    sub.add_parser("digest", help="send the weekly summary to Telegram now")

    sub.add_parser("tick", help="the background engine's 5-minute step (installed by `install`)")
    sub.add_parser("inbox", help="background job: read replies (what the engine runs every 20 minutes)")
    sub.add_parser("assist", help="background job: dashboard actions that need Gemini (regenerate, 1-page plan)")
    sub.add_parser("doctor", help="check why the engine isn't running")
    sub.add_parser("groq-check", help="test the Groq backup key with one tiny request per model")
    p = sub.add_parser("install", help="run the engine (and dashboard) in the background, no terminal needed")
    p.add_argument("--force", action="store_true", help="install even inside Desktop/Documents/Downloads")
    p.add_argument("--port", type=int, default=None, help="dashboard port (default: DASHBOARD_PORT from .env, else 7347)")
    sub.add_parser("uninstall", help="remove the background engine")

    sub.add_parser("dashboard-sync", help="apply dashboard actions, then push a fresh snapshot (cron, every 10 min)")

    p = sub.add_parser("dashboard", help="run the dashboard locally on http://127.0.0.1:7347")
    p.add_argument("--port", type=int, default=None, help="default: DASHBOARD_PORT from .env, else 7347")

    sub.add_parser("telegram-setup", help="find your Telegram chat id and send a test message")

    p = sub.add_parser("zoho-token", help="exchange a Zoho Self Client code for the refresh token .env needs")
    p.add_argument("code", help="the code from api-console.zoho.in -> Self Client -> Generate Code")

    p = sub.add_parser("zoho-check", help="test Zoho API access for each zoho_api inbox")
    p.add_argument("--send-test", metavar="EMAIL", help="also send a test email to this address")
    p.add_argument("--probe", action="store_true", help="self-addressed send, threaded follow-up, and reply check")

    p = sub.add_parser("prospect-find", help="enrich a single prospect and identify professional email")
    p.add_argument("--first", required=True, help="first name")
    p.add_argument("--last", default="", help="last name")
    p.add_argument("--company", required=True, help="company name")
    p.add_argument("--domain", default=None, help="company domain if known")
    p.add_argument("--title", default=None, help="job title")
    p.add_argument("--linkedin", default=None, help="LinkedIn profile URL")

    p = sub.add_parser("prospect-enrich", help="bulk enrich prospects from a CSV file")
    p.add_argument("csv", type=Path, help="input CSV path")
    p.add_argument("--output", type=Path, default=None, help="optional export CSV path")
    p.add_argument("--target", type=int, default=None, help="daily target verified prospects to collect (default 38)")
    p.add_argument("--workers", type=int, default=3, help="concurrency worker count")

    sub.add_parser("prospect-stats", help="display prospecting pipeline database counts and confidence distribution")

    p = sub.add_parser("prospect-export", help="export prospects to a CSV file")
    p.add_argument("output", type=Path, help="output CSV destination")
    p.add_argument("--status", choices=["verified", "high_confidence", "uncertain", "all"], default="verified")

    args = ap.parse_args()
    if getattr(args, "mock", False):
        # --mock never touches real data: it runs on a throwaway copy of the database.
        import os
        import shutil
        real, mock_db = config.db_path(), config.DATA_DIR / "mock.db"
        mock_db.parent.mkdir(parents=True, exist_ok=True)
        if real.exists():
            shutil.copyfile(real, mock_db)
        elif mock_db.exists():
            mock_db.unlink()
        os.environ["OUTREACH_DB"] = str(mock_db)
        print(f"mock run on {mock_db} (a copy); your real database is untouched")
    db.init()
    if not getattr(args, "mock", False):
        with db.connect() as conn:
            if n := db.purge_mock(conn):
                print(f"removed placeholder drafts from an earlier --mock run on {n} leads; they will be re-drafted for real")

    if args.cmd == "init":
        print("database ready")
    elif args.cmd == "prospect":
        prospect.run(args.source)
    elif args.cmd == "import":
        added, skipped = importer.import_csv(args.csv, args.segment, args.source)
        print(f"imported {added}, skipped {skipped} (duplicates, suppressed or missing email+website)")
    elif args.cmd == "enrich":
        print(f"enriched {enrich.run(args.limit)} leads")
    elif args.cmd == "verify":
        print(verify.run())
    elif args.cmd == "research":
        print(research.run(args.limit, args.mock, getattr(args, "segment", None)))
    elif args.cmd == "draft":
        print(f"drafted {personalize.run(args.limit, args.mock, getattr(args, "segment", None))} sequences")
    elif args.cmd == "prepare":
        def prepare():
            found = prospect.run() if not args.skip_prospect else {}
            enrich.run(args.limit * 4)
            checked = verify.run()
            print(checked)
            limit, held = args.limit, ""
            if not args.mock:
                r = sender.draft_room()
                if r["room"] < limit:
                    limit = r["room"]
                    held = (f"; drafting held to {limit}: {r['waiting']} emails already wait for review or sending "
                            f"and the inboxes send {r['capacity']} in 2 days")
                    print(held.lstrip("; "))
            # Research more than we draft (the fit filter drops some), minus what's already researched.
            with db.connect() as conn:
                ready = conn.execute("SELECT COUNT(*) FROM leads WHERE status='researched'").fetchone()[0]
            researched = research.run(max(0, int(limit * 1.3) - ready), args.mock) if limit else {"researched": 0}
            print(researched)
            drafted = personalize.run(limit, args.mock) if limit else 0
            print(f"drafted {drafted} sequences. Next: review them in the dashboard (or python -m outreach review)")
            new = sum(v for v in found.values() if isinstance(v, int))
            summary = f"{new} new companies, {researched.get('researched', 0)} researched, {drafted} drafted{held}"
            from . import llm
            if llm.last_stop:
                at = engine.schedule_retry("prepare", llm.last_stop)
                why = "Gemini was overloaded" if llm.last_stop == "busy" else "Gemini's free quota ran out"
                summary += f". {why}; it continues by itself at {at:%H:%M} IST"
            return summary
        prepare() if args.mock else engine.run_job("prepare", prepare)
    elif args.cmd == "review":
        review.interactive()
    elif args.cmd == "approve":
        print(f"approved {review.bulk_approve(args.min_confidence)} sequences")
    elif args.cmd == "community":
        def check_posts():
            n = community.run(args.mock)
            print(f"{n} new relevant posts -> data/opportunities_today.md")
            return f"{n} new relevant posts"
        check_posts() if args.mock else engine.run_job("community", check_posts)
    elif args.cmd == "post-done":
        with db.connect() as conn:
            conn.execute("UPDATE posts SET status='done' WHERE id=?", (args.post_id,))
        community.write_digest(config.DATA_DIR / "opportunities_today.md")
    elif args.cmd == "adlib":
        path = config.DATA_DIR / "adlibrary_today.md"
        print(f"{sources.adlibrary_tasks(path)} searches written to {path}")
    elif args.cmd == "linkedin":
        path = config.DATA_DIR / "linkedin_today.md"
        print(f"{review.linkedin_tasks(path)} LinkedIn tasks written to {path}")
    elif args.cmd == "send":
        print(f"sent {sender.tick(args.max, args.dry_run)}")
    elif args.cmd == "sync":
        print(f"processed {replies.sync(args.days, args.mock)} replies")
    elif args.cmd == "reply":
        replies.send_reply(args.reply_id)
    elif args.cmd == "report":
        report.run()
    elif args.cmd == "done":
        with db.connect() as conn:
            conn.execute("UPDATE replies SET handled=1 WHERE id=?", (args.reply_id,))
    elif args.cmd == "suppress":
        with db.connect() as conn:
            db.suppress(conn, args.email, "manual")
        print(f"suppressed {args.email}")
    elif args.cmd == "pause":
        with db.connect() as conn:
            db.set_state(conn, "sending_paused", "1")
        print("sending paused")
    elif args.cmd == "resume":
        with db.connect() as conn:
            db.set_state(conn, "sending_paused", "0")
        print("sending resumed")
    elif args.cmd == "content":
        from . import growth
        engine.run_job("content", growth.linkedin_posts)
    elif args.cmd == "digest":
        from . import growth
        print(growth.weekly_digest())
    elif args.cmd == "tick":
        engine.tick()
    elif args.cmd == "inbox":
        engine.run_job("inbox", lambda: f"{replies.sync(4)} new replies")
    elif args.cmd == "assist":
        dashboard_sync.init_remote()
        engine.run_job("assist", lambda: f"{dashboard_sync.pull(slow=True)} actions applied")
        engine.kick_tick()  # show the results in the dashboard now, not at the next 5-minute run
    elif args.cmd == "doctor":
        engine.doctor()
    elif args.cmd == "groq-check":
        groq_check()
    elif args.cmd == "install":
        engine.install(args.force, args.port)
    elif args.cmd == "uninstall":
        engine.uninstall()
    elif args.cmd == "dashboard-sync":
        dashboard_sync.sync()
    elif args.cmd == "dashboard":
        from . import dashboard_local
        dashboard_local.serve(args.port)
    elif args.cmd == "telegram-setup":
        replies.telegram_setup()
    elif args.cmd == "zoho-token":
        zoho_token(args.code)
    elif args.cmd == "zoho-check":
        zoho_check(args.send_test, getattr(args, "probe", False))
    elif args.cmd == "prospect-find":
        proc = prospecting.ProspectProcessor()
        inp = prospecting.ProspectInput(
            first_name=args.first, last_name=args.last, company=args.company,
            domain=args.domain, title=args.title, linkedin_url=args.linkedin
        )
        res = proc.process(inp)
        print(f"\nResult for {res.full_name} ({res.company}):")
        print(f"  Email:      {res.final_email or '(none)'}")
        print(f"  Confidence: {res.confidence_score}/100 [{res.confidence_level}]")
        print(f"  Status:     {res.email_status}")
        print(f"  Source:     {res.source}")
        if res.source_url:
            print(f"  Source URL: {res.source_url}")
        print(f"  Pattern:    {res.email_pattern or 'n/a'}")
        print("  Evidence:")
        for ev in res.evidence:
            print(f"    - [{ev.type}] {ev.detail} ({ev.weight:+d})")

    elif args.cmd == "prospect-enrich":
        inputs = prospecting.load_prospects_from_csv(args.csv)
        print(f"Loaded {len(inputs)} prospect(s) from {args.csv}")
        bp = prospecting.BulkProcessor(max_workers=args.workers, target_verified=args.target)
        def _prog(r, i, total):
            print(f"  [{i:>3}/{total}] {r.full_name[:22]:<22} | {r.company[:20]:<20} -> {r.final_email or '(none)':<28} [{r.confidence_score:>2}/100 {r.email_status}]")
        results = bp.process_batch(inputs, on_progress=_prog)
        verified = sum(1 for r in results if r.confidence_level in ("verified", "high_confidence") and r.final_email)
        print(f"\nProcessed {len(results)} prospects: {verified} verified/high-confidence")
        if args.output:
            out_p = bp.export_to_csv(results, args.output)
            print(f"Exported results to {out_p}")

    elif args.cmd == "prospect-stats":
        with db.connect() as conn:
            total = db.count_prospects(conn)
            print(f"\nTotal Prospects in DB: {total}")
            print("\nBy Status:")
            for row in conn.execute("SELECT email_status, count(*) FROM prospects GROUP BY email_status ORDER BY count(*) DESC"):
                print(f"  {row[0]:<20} {row[1]}")
            print("\nBy Confidence Level:")
            for row in conn.execute("SELECT confidence_level, count(*) FROM prospects GROUP BY confidence_level ORDER BY count(*) DESC"):
                print(f"  {row[0]:<20} {row[1]}")
            print("\nCached Domains:")
            dom_cnt = conn.execute("SELECT count(*) FROM prospect_domain_cache").fetchone()[0]
            print(f"  {dom_cnt} domain patterns cached")
            print("\nProvider Credits Used This Month:")
            from datetime import datetime, timezone
            start_month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0).isoformat()
            for row in conn.execute("SELECT provider, sum(credits_used) FROM provider_credits WHERE request_timestamp >= ? GROUP BY provider", (start_month,)):
                print(f"  {row[0]:<15} {row[1]} credits")

    elif args.cmd == "prospect-export":
        with db.connect() as conn:
            status_filter = None if args.status == "all" else args.status
            rows = db.list_prospects(conn, status=status_filter, limit=10000)
        import csv
        fieldnames = ["id", "full_name", "company", "title", "domain", "final_email", "confidence_score", "email_status", "source", "source_url", "linkedin_url"]
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        print(f"Exported {len(rows)} prospect(s) to {args.output}")


def zoho_token(code: str) -> None:
    boxes = [b for b in config.inboxes(include_disabled=True) if b.get("transport") == "zoho_api"]
    if not boxes:
        raise SystemExit("no inbox in settings.yaml uses transport: zoho_api")
    z = transport.zoho(boxes[0])
    if not (z.client_id and z.client_secret):
        raise SystemExit("Set ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in .env first (from the Self Client's Client Secret tab).")
    data = z.exchange_code(code)
    if "refresh_token" not in data:
        err = data.get("error", data)
        tips = {"invalid_code": "The code expired or was already used. Generate a new one (choose 10 minutes) "
                                "and run this straight away.",
                "invalid_client": transport.ZOHO_HINTS["invalid_client"].strip()}
        raise SystemExit(f"Zoho refused the code: {err}\n{tips.get(err, '')}".rstrip())
    if "ZohoMail.messages" not in data.get("scope", "ZohoMail.messages"):
        print(f"Warning: this token's scopes are {data.get('scope')}; sending needs "
              "ZohoMail.accounts.READ,ZohoMail.messages.ALL,ZohoMail.folders.READ")
    print("Put this line in .env (replace the old ZOHO_REFRESH_TOKEN):\n")
    print(f"ZOHO_REFRESH_TOKEN={data['refresh_token']}\n")
    print("Then run: python -m outreach zoho-check --send-test YOUR_GMAIL@gmail.com")


def zoho_check(send_to: str | None, probe: bool = False) -> None:
    boxes = [b for b in config.inboxes(include_disabled=True) if b.get("transport") == "zoho_api"]
    if not boxes:
        print("no inbox in settings.yaml uses transport: zoho_api")
    for box in boxes:
        z = transport.zoho(box)
        try:
            z.token()
            print(f"{box['email']}: token OK")
            print(f"{box['email']}: account id {z.account_id()}")
            print(f"{box['email']}: read {len(z.fetch(2))} recent inbox messages")
            if send_to:
                mid, pid = z.send(send_to, "zoho api test", "Test from agent-outreach.", None)
                print(f"{box['email']}: test email sent (message id {mid or '?'}, provider id {pid or '?'})")
                if not pid:
                    print("  note: no message id returned, so follow-ups will go as new emails with 'Re:' subjects")
            if probe:
                _run_zoho_probe(box, z)
        except (transport.ZohoError, OSError) as e:
            print(f"{box['email']}: FAILED -> {e}")
            print("  If this says the feature needs a paid plan, send from Gmail over SMTP instead (see README).")


def _run_zoho_probe(box: dict, z: transport.Zoho) -> None:
    import uuid
    from datetime import datetime, timezone
    tag = uuid.uuid4().hex[:8]
    print(f"\n--- Running self-addressed Zoho probe ({tag}) ---")
    subj = f"probe: agent-outreach check {tag}"
    mid, pid = z.send(box["email"], subj, f"Self-test step 0 (tag: {tag}).", None)
    if not mid or not pid:
        raise transport.ZohoError(f"Self-addressed send failed: mid={mid}, pid={pid}")
    print(f"  ✓ 1. Self-addressed message sent (mid: {mid}, pid: {pid})")

    fmid, fpid = z.send(box["email"], f"Re: {subj}", f"Self-test follow-up (tag: {tag}).",
                        {"message_id": mid, "provider_id": pid})
    if not fmid or not fpid:
        raise transport.ZohoError(f"Threaded follow-up failed: fmid={fmid}, fpid={fpid}")
    print(f"  ✓ 2. Threaded follow-up sent (mid: {fmid}, pid: {fpid})")

    test_email = f"probe-{tag}@example.com"
    with db.connect() as conn:
        db.add_lead(conn, email=test_email, domain="example.com", company="Probe Corp",
                    segment="uk_agencies", status="active", fit=8, email_status="valid", email_source="website")
        lead = conn.execute("SELECT id FROM leads WHERE email=?", (test_email,)).fetchone()[0]
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, message_id, provider_id, sent_at) "
                     "VALUES (?, 0, ?, 'step 0', 'sent', ?, ?, ?)", (lead, subj, mid, pid, db.now()))
        conn.execute("INSERT INTO messages (lead_id, step, subject, body, status, confidence, due_at) "
                     "VALUES (?, 1, ?, 'step 1', 'approved', 0.9, ?)",
                     (lead, f"Re: {subj}", (datetime.now(timezone.utc)).isoformat()))
        incoming = transport.Incoming(f"<reply-{tag}>", test_email, "Probe Contact",
                                      f"Re: {subj}", "Yes, interested.", db.now(), refs=mid, provider_id=fpid)
        lead_row = replies._match_lead(conn, incoming, is_bounce=False)
        if not lead_row or lead_row["id"] != lead:
            raise RuntimeError("Reply matching failed to link incoming message to active lead thread")
        replies._apply(conn, lead_row, replies.ReplyClass(category="interested", summary="interested", suggested_reply="Great"))
        m1 = conn.execute("SELECT status FROM messages WHERE lead_id=? AND step=1", (lead,)).fetchone()
        if m1[0] != "cancelled":
            raise RuntimeError(f"Follow-up step 1 was not cancelled after reply (status: {m1[0]})")
        conn.execute("DELETE FROM messages WHERE lead_id=?", (lead,))
        conn.execute("DELETE FROM leads WHERE id=?", (lead,))
        print("  ✓ 3. Reply matched to thread, sequence stopped, follow-up cancelled.")
    print("✓ All self-addressed Zoho probe checks passed!\n")


if __name__ == "__main__":
    main()


def groq_check() -> None:
    from pydantic import BaseModel

    from . import groq

    class Ping(BaseModel):
        ok: bool
        word: str
    if not groq.enabled():
        raise SystemExit("GROQ_API_KEY is not set in .env (free key: console.groq.com/keys)")
    for m in groq.models():
        result = groq.try_model(m, "You answer health checks.", 'Reply with ok=true and word="ready".', Ping, 0)
        detail = result[1] if result[0] == "ok" else " ".join(map(str, result[1:]))
        print(f"{m.removeprefix('groq/'):<28} {result[0]:<12} {detail}")
    print("Models marked ok are used automatically whenever Gemini can't answer.")
