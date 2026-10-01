"""Command line entry point: `python -m outreach <command>`."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import (community, config, dashboard_sync, db, engine, enrich, importer, personalize, prospect, replies,
               report, research, review, sender, sources, transport, verify)


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

    p = sub.add_parser("draft", help="write sequences for today's best researched leads")
    p.add_argument("--limit", type=int, default=38)
    p.add_argument("--mock", action="store_true", help="placeholder drafts, no API calls")

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

    sub.add_parser("tick", help="the background engine's 5-minute step (installed by `install`)")
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
        print(research.run(args.limit, args.mock))
    elif args.cmd == "draft":
        print(f"drafted {personalize.run(args.limit, args.mock)} sequences")
    elif args.cmd == "prepare":
        def prepare():
            found = prospect.run() if not args.skip_prospect else {}
            enrich.run(args.limit * 4)
            checked = verify.run()
            print(checked)
            # Research more than we draft: the fit filter drops some.
            researched = research.run(int(args.limit * 1.3), args.mock)
            print(researched)
            drafted = personalize.run(args.limit, args.mock)
            print(f"drafted {drafted} sequences. Next: review them in the dashboard (or python -m outreach review)")
            new = sum(v for v in found.values() if isinstance(v, int))
            return f"{new} new companies, {researched.get('researched', 0)} researched, {drafted} drafted"
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
    elif args.cmd == "tick":
        engine.tick()
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
        zoho_check(args.send_test)


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
    print(f"Then run: python -m outreach zoho-check --send-test YOUR_GMAIL@gmail.com")


def zoho_check(send_to: str | None) -> None:
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
        except (transport.ZohoError, OSError) as e:
            print(f"{box['email']}: FAILED -> {e}")
            print("  If this says the feature needs a paid plan, send from Gmail over SMTP instead (see README).")


if __name__ == "__main__":
    main()
