# agent-outreach

A personalised cold-email engine: **38 researched prospects a day**, each on a 4-email threaded sequence, sent from warmed inboxes inside the prospect's business hours. Replies are triaged automatically, and your answer is already drafted when you open the inbox.

The strategy (niche, offers, infrastructure, 30-day calendar) is in **[PLAYBOOK.md](PLAYBOOK.md)**. This file covers running the tool.

## How it works

| Step | Command | Runs | What it does |
|---|---|---|---|
| Import | `import leads.csv --segment realestate_in` | you, 2-3x/week | Loads leads; one per company domain; skips suppressed addresses |
| Enrich | `enrich` | cron (in `prepare`) | Reads home/about/contact/careers pages: WhatsApp links, CRM/chat tools, portals, hiring, emails on the site |
| Verify | `verify` | cron (in `prepare`) | Syntax, MX and role-address checks; scores leads per segment |
| Draft | `draft` | cron (in `prepare`) | Claude writes email 1 + 3 follow-ups from the signals, your profile and the segment playbook |
| Review | `review` / `approve --min-confidence 0.85` | **you, ~20 min/day** | Approve, edit in `$EDITOR`, regenerate or reject |
| Send | `send` | cron, every 10 min | 2 per run, 7-13 min gaps, per-inbox warm-up ramp, segment time zones, threaded follow-ups on days 3/7/14, pauses an inbox above 3% bounces |
| Sync | `sync` | cron, every 15 min | IMAP replies → stop the sequence → classify → suppress opt-outs → save a threaded draft reply → Telegram alert |
| Report | `report` | you | Funnel per segment and positive replies waiting on you |

## Setup (about 30 minutes, after the domains and inboxes in PLAYBOOK §4)

```bash
git clone <this repo> && cd agent-outreach
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                                  # API key + inbox app passwords
cp config/settings.example.yaml config/settings.yaml  # inboxes, segments, quotas, ramp
cp config/profile.example.yaml  config/profile.yaml   # your facts and proof points: check every line

python -m outreach init
python -m outreach import data/leads_template.csv     # replace with your real lists
python -m outreach prepare --mock                     # pipeline check without API calls
python -m outreach prepare                            # real drafts
python -m outreach review
python -m outreach send --dry-run                     # shows what would go out right now
```

Then install `scripts/crontab.example` on a machine that stays on (laptop or a free VM).

## CSV format

`first_name, last_name, title, company, website, email, city, country, segment, notes`. Common export headers (Google Maps scrapers, Apollo, Hunter) are recognised too. **`notes` is the most valuable column**: one thing you noticed about the lead ("raised seed Aug 2026", "running FB ads for Sector 150") gives the draft its hook.

Rows with no email but a website get one from the site's contact pages. Rows with only a name and domain get a pattern guess (`first.last@`), and guessed emails are **not** drafted unless you pass `draft --allow-guessed`.

## Useful commands

```bash
python -m outreach report                 # funnel + open positive replies
python -m outreach done 12                # mark reply #12 handled
python -m outreach suppress @competitor.com   # never email a domain
OUTREACH_MODEL=claude-sonnet-5-5 python -m outreach draft   # cheaper drafting
```

## Notes

- State lives in `data/outreach.db` (SQLite, git-ignored). The crontab keeps a rolling 7-day backup.
- Drafting uses `claude-opus-5-5` by default (about $2/day for 38 sequences). The system prompt is cached across leads. A refused or unparseable draft is skipped and retried on the next run.
- Nothing is sent that you haven't approved. Follow-ups are approved together with email 1 and stop automatically on any reply except out-of-office.
