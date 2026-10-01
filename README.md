# agent-outreach

A zero-budget, automated cold-outreach engine. It finds companies from free public sources (startup directories, remote job boards, UK and Dubai business registers, Hacker News, OpenStreetMap), researches each one with Gemini, writes a personalised email plus follow-ups and LinkedIn texts, sends from Gmail or Zoho within each market's business hours, and triages the replies.

Strategy, market research and the 30-day plan are in **[PLAYBOOK.md](PLAYBOOK.md)**.
What turns replies into contracts, and the channels beyond cold email (case-study page, Loom, referrals, Upwork, LinkedIn, internships), is in **[GROWTH.md](GROWTH.md)**.

## Pipeline

| Step | Command | Runs | What it does |
|---|---|---|---|
| Prospect | `prospect` | cron (in `prepare`) | YC directory (hiring startups), HN "Who is hiring" / "Seeking freelancer" / "Launch HN" posts, remote job boards (Remotive, Himalayas, RemoteOK, Jobicy, We Work Remotely), UK Companies House (agencies + director names), Dubai Land Department broker register, OpenStreetMap businesses, optional Google Maps via Apify |
| Ad Library | `adlib` | cron → you | `data/adlibrary_today.md`: 3 Meta Ad Library searches for brokerages running click-to-WhatsApp ads; you add ~10 to `data/adlibrary.csv` and `import --source adlibrary` |
| Community | `community` | cron, every 30 min | New "[Hiring]" posts on the n8n forum and Reddit → Gemini checks fit and drafts a reply → `data/opportunities_today.md` + Telegram. **You reply by hand**, then `post-done <id>` |
| Import | `import leads.csv --segment X` | you, optional | Add your own lists |
| Enrich | `enrich` | cron | Reads home/about/contact/services/careers/team pages: signals + published emails. Firecrawl steps in only for JavaScript-only sites and to find contact/team pages at unusual addresses (daily credit cap) |
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

Run each block in a terminal. The commands carry no inline `# notes`, because the Mac's default
shell (zsh) passes pasted notes to the command as extra arguments.

```bash
git clone <this repo> && cd agent-outreach
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
cp config/profile.example.yaml config/profile.yaml
python -m outreach init
python -m pytest -q tests
```

Fill in `.env` (keys), `config/profile.yaml` (your facts and signatures) and `config/settings.yaml`
(inbox, `warmup_start`). Then check the connections. Run `zoho-check --probe` to verify the provider
and application path (self-addressed send, threaded follow-up, and reply sequence cancellation).
Send your Telegram bot one message before `telegram-setup`, then copy the `TELEGRAM_CHAT_ID` line it prints into `.env`.

```bash
python -m outreach zoho-check --probe
python -m outreach telegram-setup
```

First run: `prepare --mock` checks the pipeline without Gemini, `prepare` does the real prospect ->
research -> draft run, `review` is where you approve, and `send --dry-run` previews without mutating the database.

```bash
python -m outreach prepare --mock
python -m outreach prepare
python -m outreach review
python -m outreach send --dry-run
```

## Hands-free: run it in the background and use only the dashboard

```bash
python -m outreach install
```

This is the last terminal command you need. On a Mac it registers a background job with launchd that runs `python -m outreach tick` every 5 minutes while the Mac is awake and you're logged in. When the dashboard variables are set in `.env`, it also keeps the dashboard running at http://127.0.0.1:7347 (change it with `DASHBOARD_PORT` in `.env`). On Linux it prints the one crontab line to add instead.

Each tick:
- applies what you did in the dashboard;
- syncs every sending inbox before any outbound send cycle;
- sends due emails inside each market's hours (held if inbox sync fails, or if sending is paused);
- records a durable `sending` state before network transport so crashes never cause duplicates;
- gates sends to `allowed_segments` (UK agencies and India startup internships by default);
- reads replies every 20 minutes and checks community boards every 30;
- runs `prepare` (find, research, draft) every morning at 07:30, Mon-Sat;
- pushes a fresh snapshot to the dashboard.

In the dashboard's **Engine** tab you can pause or resume sending, run "find & draft" or the community check now, see each job's last result and output, open today's Ad Library searches and add advertisers, and copy today's LinkedIn texts.

macOS blocks background jobs from reading `~/Desktop`, `~/Documents` and `~/Downloads`, so `install` refuses to run from there and prints the commands to move the project to `~/agent-outreach`. To stop it: `python -m outreach uninstall`.

**Backup AI (optional, free):** Gemini's free tier is small and often overloaded. Add `GROQ_API_KEY=...` to `.env` (free key at console.groq.com/keys) and every AI step switches to Groq whenever no Gemini model can answer. Check it with `python -m outreach groq-check`. The Engine tab's AI card shows which Groq models were used.

**If the dashboard says "Engine stopped":** the Mac is usually asleep (background jobs pause during sleep). Wake it and press ↻ in the dashboard: that asks the Mac to sync right away, and actions you take in the dashboard apply within about a minute. If it stays red while the Mac is awake, run `python -m outreach doctor`. It says what's wrong and starts the engine.

`--mock` runs (`prepare --mock` etc.) work on a throwaway copy, `data/mock.db`, and never change real data. Placeholder text is also blocked from ever being sent.

## Sending: Gmail vs Zoho
- **Gmail** (`transport: smtp`): free, and works with an app password. Replies are read over IMAP and drafted answers land in Gmail Drafts.
- **Zoho free plan**: webmail only, with no SMTP/IMAP. The `zoho_api` transport uses Zoho's REST API with a Self Client refresh token. Whether your plan allows it is only knowable by trying: `zoho-check`. Follow-ups thread through the API's reply action when Zoho returns a message id; otherwise they go as new emails with a "Re:" subject.
- **Zoho paid plan**: use `transport: smtp` with `smtp.zoho.in:465` / `imap.zoho.in`.

## Dashboard (Vercel)
A password-protected web page for the daily work: **Review** drafts (edit, approve, regenerate, reject), **Respond** to positive replies (send from the page) and community posts (copy the draft), **Results** per segment and lead source, and **Activity**. It works on a phone.

The engine and API keys stay on your laptop. Each five-minute tick applies dashboard actions and pushes a snapshot to Turso. The snapshot includes draft emails, reply bodies, lead contact details, and recent job log lines; review what those logs contain. The page needs your laptop awake to apply actions or send mail.

Setup, about 10 minutes:
1. **Turso** (free): sign up at turso.tech, create a database, and copy its URL (`libsql://...`). Create a token for that database. Put both in `.env` as `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`, then run `python -m outreach dashboard-sync` once. It creates the tables and pushes the first snapshot.
2. **Vercel** (free): Add New -> Project -> import this GitHub repo.
   - **Root Directory:** `dashboard`. **Framework preset:** Other.
   - **Environment variables:** `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `DASHBOARD_PASSWORD` (long; anyone with it can send replies from your inbox) and `SESSION_SECRET` (generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
   - Deploy.
   - Vercel publishes the repo's default branch as production. If this code is still on another branch, merge it or set Settings -> Git -> Production Branch.
3. Run `python -m outreach install`. The engine's tick syncs the dashboard every 5 minutes.

To use it without Vercel: `python -m outreach dashboard` serves the same page at http://127.0.0.1:7347 (it needs the four variables above in `.env`).

## Offers, A/B angles, deals and the weekly summary
- Each segment in `config/settings.example.yaml` has a fixed-price `offer` and `price` (defaults to check, not agreed quotes) and two `angles`. Every lead gets the angle used least so far in its segment, and **Results → A/B test** shows which one gets replies.
- **Respond → Deals:** move each conversation through call booked → proposal sent → won (with value and currency) or lost. Record the next action and due date. **Make 1-page plan** on a reply writes a priced draft from that lead's research and their reply; check it before sending.
- Mondays: three LinkedIn post drafts at 08:00 (Engine tab) and a summary on Telegram at 09:00 with last week's numbers and the changes to make. Run them any time with `python -m outreach content` and `python -m outreach digest`.
- `site/index.html` is your case-study page; deploy it free (see GROWTH.md).

## Firecrawl
Set `FIRECRAWL_API_KEY` (or `FIRECRAWL_API_URL` for a self-hosted copy). It is used only when the free path fails:
- **scrape:** a website that downloads empty or as a JavaScript shell is rendered, so research has real text.
- **map:** finds the real contact/about/team/careers pages when they aren't at the usual addresses.
- **search:** finds a company's website from its name (job boards, Companies House, Dubai register) after domain guessing fails. The page must still name the company.

`firecrawl.daily_credit_cap` in `settings.yaml` limits spend (default 60/day). At the cap, or when the account is out of credits, the engine carries on without it.

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
