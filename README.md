# agent-outreach

A zero-budget, automated cold-outreach engine. It finds companies from free public sources (startup directories, remote job boards, UK and Dubai business registers, Hacker News, OpenStreetMap), researches each one with Gemini, writes a personalised email plus follow-ups and LinkedIn texts, sends from Gmail or Zoho within each market's business hours, and triages the replies.

Strategy, market research and the 30-day plan are in **[PLAYBOOK.md](PLAYBOOK.md)**.

## Pipeline

| Step | Command | Runs | What it does |
|---|---|---|---|
| Prospect | `prospect` | cron (in `prepare`) | YC directory (hiring startups), HN "Who is hiring" / "Seeking freelancer" / "Launch HN" posts, remote job boards (Remotive, Himalayas, RemoteOK, Jobicy, We Work Remotely), UK Companies House (agencies + director names), Dubai Land Department broker register, OpenStreetMap businesses, optional Google Maps via Apify |
| Ad Library | `adlib` | cron → you | `data/adlibrary_today.md`: 3 Meta Ad Library searches for brokerages running click-to-WhatsApp ads; you add ~10 to `data/adlibrary.csv` and `import --source adlibrary` |
| Community | `community` | cron, every 30 min | New "[Hiring]" posts on the n8n forum and Reddit → Gemini checks fit and drafts a reply → `data/opportunities_today.md` + Telegram. **You reply by hand**, then `post-done <id>` |
| Import | `import leads.csv --segment X` | you, optional | Add your own lists |
| Enrich | `enrich` | cron | Reads home/about/contact/services/careers/team pages: signals + published emails |
| Verify | `verify` | cron | Syntax, MX and role-address checks. Only published emails are ever sent |
| Research | `research` | cron | Gemini brief from source post + website + Google News headlines; fit score 0-10, under 6 dropped |
| Draft | `draft` | cron | Gemini writes email 1 + 2 follow-ups + a LinkedIn note/DM; India share capped at 25% |
| Review | `review` / `approve --min-confidence 0.85` | **you, ~20 min/day** | Approve, edit, regenerate or reject |
| LinkedIn | `linkedin` | cron → you | `data/linkedin_today.md`: people-search links + texts to send **by hand** |
| Send | `send` | cron, every 10 min | 2 per run, 8-15 min gaps, per-inbox warm-up ramp, market time zones, threaded follow-ups, bounce auto-pause |
| Sync | `sync` | cron, every 20 min | Replies → stop sequence → Gemini triage → suppress opt-outs → draft answer → Telegram |
| Reply | `reply <id>` | you | Edit and send a drafted answer (needed for Zoho API inboxes) |
| Report | `report` | you | Funnel per segment + positive replies waiting on you |

## Setup

```bash
git clone <this repo> && cd agent-outreach
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                                  # Gemini key, Gmail app password, Zoho API creds
cp config/settings.example.yaml config/settings.yaml  # inboxes, segments, quotas, sources, cities
cp config/profile.example.yaml  config/profile.yaml   # your facts, proof points, signatures

python -m outreach init
python -m outreach zoho-check --send-test you@gmail.com   # only if using the Zoho API inbox
python -m outreach prepare --mock                     # pipeline check without Gemini calls
python -m outreach prepare                            # real: prospect -> research -> drafts
python -m outreach review
python -m outreach send --dry-run
python -m pytest -q tests                             # offline tests
```

Then install `scripts/crontab.example` on a machine that's on from 09:00 to midnight IST (the US morning is your evening).

## Sending: Gmail vs Zoho
- **Gmail** (`transport: smtp`): free, and works with an app password. Replies are read over IMAP and drafted answers land in Gmail Drafts.
- **Zoho free plan**: webmail only, with no SMTP/IMAP. The `zoho_api` transport uses Zoho's REST API with a Self Client refresh token. Whether your plan allows it is only knowable by trying: `zoho-check`. Follow-ups thread through the API's reply action when Zoho returns a message id; otherwise they go as new emails with a "Re:" subject.
- **Zoho paid plan**: use `transport: smtp` with `smtp.zoho.in:465` / `imap.zoho.in`.

## Tuning
- Prompts: `prompts/research_system.md`, `prompts/draft_system.md`, `prompts/reply_system.md`.
- Markets and offers: `segments` in `config/settings.yaml` (audience, pain, offer, CTA, market style, send windows, daily quota).
- Sources: `prospecting` jobs, `community` boards, `adlibrary` searches and `cities` bounding boxes in `config/settings.yaml`. Setup for the keyed sources (Companies House, Reddit, Apify) and the Dubai CSV is in PLAYBOOK §3a. Run one source on its own with `prospect --source jobs`.
- Models and pace: `GEMINI_MODEL`, `GEMINI_RESEARCH_MODEL`, `GEMINI_REPLY_MODEL`, `GEMINI_RPM` in `.env`.

## Deliberate limits
- **No automated Facebook, LinkedIn, Justdial, IndiaMART, Clutch or Wellfound collection.** Their terms forbid it. The Ad Library step is by hand for that reason.
- **No LinkedIn automation or scraping.** It breaks LinkedIn's terms and gets accounts restricted (see PLAYBOOK §3). The tool writes the texts and links; you send about 10/day by hand.
- **No guessed emails.** Only addresses published by the company (website, OSM listing, its own post) with a working mail domain.
- **Nothing is sent without your approval.** Follow-ups are approved together with email 1 and stop on any reply except out-of-office.
