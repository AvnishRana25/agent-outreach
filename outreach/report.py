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
        won_value = conn.execute(
            f"SELECT COALESCE(SUM(deal_value), 0) FROM leads l {where} {'AND' if where else 'WHERE'} "
            "l.deal_stage='won'", args).fetchone()[0]
        rows.append({"name": g, "leads": q("1=1"), "queued": q("l.status IN ('drafted','approved')"),
                     "sent": sent, "replied": replied, "positive": positive, "bounced": q("r.category='bounce'"),
                     "calls": q("l.deal_stage IN ('call_booked','proposal_sent','won')"),
                     "proposals": q("l.deal_stage IN ('proposal_sent','won')"), "won": q("l.deal_stage='won'"),
                     "won_value": round(won_value or 0, 2),
                     "reply_rate": round(100 * replied / sent, 1) if sent else None,
                     "positive_rate": round(100 * positive / sent, 1) if sent else None})
    return rows


def angles(conn) -> list[dict]:
    """A/B results: for each segment and angle, first emails sent and how many replied."""
    rows = conn.execute(f"""
        SELECT l.segment, l.angle,
               COUNT(DISTINCT CASE WHEN m.step=0 AND m.status='sent' THEN l.id END) AS sent,
               COUNT(DISTINCT CASE WHEN r.category NOT IN ('bounce','out_of_office') THEN l.id END) AS replied,
               COUNT(DISTINCT CASE WHEN r.category IN {POSITIVE_SQL} THEN l.id END) AS positive
        FROM leads l LEFT JOIN messages m ON m.lead_id=l.id LEFT JOIN replies r ON r.lead_id=l.id
        WHERE l.angle != '' GROUP BY l.segment, l.angle ORDER BY l.segment, l.angle""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reply_rate"] = round(100 * d["replied"] / d["sent"], 1) if d["sent"] else None
        d["positive_rate"] = round(100 * d["positive"] / d["sent"], 1) if d["sent"] else None
        out.append(d)
    return out


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
