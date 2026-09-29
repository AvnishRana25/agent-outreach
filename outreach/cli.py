"""Command line entry point: `python -m outreach <command>`."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import db, enrich, importer, personalize, replies, report, review, sender, verify


def main() -> None:
    ap = argparse.ArgumentParser(prog="outreach", description="Personalised cold email pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database")

    p = sub.add_parser("import", help="import leads from a CSV")
    p.add_argument("csv", type=Path)
    p.add_argument("--segment", help="segment for rows without a segment column")
    p.add_argument("--source", default="csv")

    p = sub.add_parser("enrich", help="scrape lead websites for personalisation signals")
    p.add_argument("--limit", type=int, default=150)

    sub.add_parser("verify", help="check emails (syntax, MX, role) and score leads")

    p = sub.add_parser("draft", help="write sequences for today's best leads")
    p.add_argument("--limit", type=int, default=38)
    p.add_argument("--mock", action="store_true", help="placeholder drafts, no API calls")
    p.add_argument("--allow-guessed", action="store_true", help="also draft for pattern-guessed emails")

    p = sub.add_parser("prepare", help="morning job: enrich + verify + draft")
    p.add_argument("--limit", type=int, default=38)
    p.add_argument("--mock", action="store_true")

    sub.add_parser("review", help="approve / edit / reject today's drafts")

    p = sub.add_parser("approve", help="bulk-approve drafts at or above a confidence")
    p.add_argument("--min-confidence", type=float, default=0.85)

    p = sub.add_parser("send", help="send due emails (run from cron every ~10 min)")
    p.add_argument("--max", type=int, default=2)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("sync", help="pull replies, stop sequences, draft answers")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--mock", action="store_true")

    sub.add_parser("report", help="funnel numbers")

    p = sub.add_parser("done", help="mark a positive reply as handled")
    p.add_argument("reply_id", type=int)

    p = sub.add_parser("suppress", help="never email this address or @domain")
    p.add_argument("email")

    args = ap.parse_args()
    db.init()

    if args.cmd == "init":
        print("database ready")
    elif args.cmd == "import":
        added, skipped = importer.import_csv(args.csv, args.segment, args.source)
        print(f"imported {added}, skipped {skipped} (duplicates, suppressed or missing email+website)")
    elif args.cmd == "enrich":
        print(f"enriched {enrich.run(args.limit)} leads")
    elif args.cmd == "verify":
        print(verify.run())
    elif args.cmd == "draft":
        print(f"drafted {personalize.run(args.limit, args.mock, args.allow_guessed)} sequences")
    elif args.cmd == "prepare":
        enrich.run(args.limit * 3)
        print(verify.run())
        print(f"drafted {personalize.run(args.limit, args.mock)} sequences. Next: python -m outreach review")
    elif args.cmd == "review":
        review.interactive()
    elif args.cmd == "approve":
        print(f"approved {review.bulk_approve(args.min_confidence)} sequences")
    elif args.cmd == "send":
        print(f"sent {sender.tick(args.max, args.dry_run)}")
    elif args.cmd == "sync":
        print(f"processed {replies.sync(args.days, args.mock)} replies")
    elif args.cmd == "report":
        report.run()
    elif args.cmd == "done":
        with db.connect() as conn:
            conn.execute("UPDATE replies SET handled=1 WHERE id=?", (args.reply_id,))
    elif args.cmd == "suppress":
        with db.connect() as conn:
            db.suppress(conn, args.email, "manual")
        print(f"suppressed {args.email}")


if __name__ == "__main__":
    main()
