"""Funnel numbers per segment, so week 2 decisions are based on data instead of feel."""
from __future__ import annotations

from . import db


POSITIVE_SQL = "('interested','meeting_request','question','referral')"


def funnel(conn, by: str = "segment") -> list[dict]:
    """One row per segment (or per lead source) plus an ALL row: the numbers the day-14 decision needs."""
    col = "l.segment" if by == "segment" else "l.source"
    groups = [r[0] for r in conn.execute(f"SELECT DISTINCT {col} FROM leads l ORDER BY 1") if r[0]]
    rows = []
    for g in groups + ["ALL"]:
        where, args = ("", ()) if g == "ALL" else (f"WHERE {col}=?", (g,))
        q = lambda extra: conn.execute(  # noqa: E731
            f"SELECT COUNT(DISTINCT l.id) FROM leads l "
            f"LEFT JOIN messages m ON m.lead_id=l.id LEFT JOIN replies r ON r.lead_id=l.id "
            f"{where} {'AND' if where else 'WHERE'} {extra}", args).fetchone()[0]
        sent = q("m.step=0 AND m.status='sent'")
        replied = q("r.category NOT IN ('bounce','out_of_office')")
        positive = q(f"r.category IN {POSITIVE_SQL}")
        rows.append({"name": g, "leads": q("1=1"), "queued": q("l.status IN ('drafted','approved')"),
                     "sent": sent, "replied": replied, "positive": positive, "bounced": q("r.category='bounce'"),
                     "reply_rate": round(100 * replied / sent, 1) if sent else None,
                     "positive_rate": round(100 * positive / sent, 1) if sent else None})
    return rows


def run() -> None:
    with db.connect() as conn:
        print(f"\n{'segment':<20}{'leads':>7}{'queued':>8}{'sent':>7}{'replied':>9}{'positive':>10}"
              f"{'bounce':>8}{'reply%':>8}{'pos%':>7}")
        for r in funnel(conn):
            pct = lambda v: "-" if v is None else f"{v:.1f}"  # noqa: E731
            print(f"{r['name']:<20}{r['leads']:>7}{r['queued']:>8}{r['sent']:>7}{r['replied']:>9}"
                  f"{r['positive']:>10}{r['bounced']:>8}{pct(r['reply_rate']):>8}{pct(r['positive_rate']):>7}")

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
