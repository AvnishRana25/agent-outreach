"""Funnel numbers per segment, so week 2 decisions are based on data instead of feel."""
from __future__ import annotations

from . import db


def run() -> None:
    with db.connect() as conn:
        segs = [r[0] for r in conn.execute("SELECT DISTINCT segment FROM leads ORDER BY 1")]
        print(f"\n{'segment':<20}{'leads':>7}{'queued':>8}{'sent':>7}{'replied':>9}{'positive':>10}"
              f"{'bounce':>8}{'reply%':>8}{'pos%':>7}")
        for seg in segs + ["ALL"]:
            where, args = ("", ()) if seg == "ALL" else ("WHERE l.segment=?", (seg,))
            q = lambda extra: conn.execute(  # noqa: E731
                f"SELECT COUNT(DISTINCT l.id) FROM leads l "
                f"LEFT JOIN messages m ON m.lead_id=l.id LEFT JOIN replies r ON r.lead_id=l.id "
                f"{where} {'AND' if where else 'WHERE'} {extra}", args).fetchone()[0]
            leads = q("1=1")
            queued = q("l.status IN ('drafted','approved')")
            sent = q("m.step=0 AND m.status='sent'")
            replied = q("r.category NOT IN ('bounce','out_of_office')")
            positive = q("r.category IN ('interested','meeting_request','question','referral')")
            bounced = q("r.category='bounce'")
            pct = lambda a: f"{100 * a / sent:.1f}" if sent else "-"  # noqa: E731
            print(f"{seg:<20}{leads:>7}{queued:>8}{sent:>7}{replied:>9}{positive:>10}{bounced:>8}"
                  f"{pct(replied):>8}{pct(positive):>7}")

        print("\nRecent sends per day and inbox:")
        for r in conn.execute("SELECT day, inbox, SUM(count) c FROM send_log GROUP BY day, inbox "
                              "ORDER BY day DESC LIMIT 30"):
            print(f"  {r['day']}  {r['inbox']:<32} {r['c']}")

        open_ = conn.execute(
            "SELECT r.*, l.company, l.first_name FROM replies r JOIN leads l ON l.id=r.lead_id "
            "WHERE r.handled=0 AND r.category IN ('interested','meeting_request','question','referral') "
            "ORDER BY r.received_at").fetchall()
        if open_:
            print("\nPositive replies waiting on you (mark done with `outreach done <id>`):")
            for r in open_:
                print(f"  #{r['id']} {r['received_at'][:16]} {r['first_name']} @ {r['company']}: {r['summary']}")
