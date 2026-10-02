# agent-outreach

> A B2B outreach desk that researches companies and contacts, drafts emails for review, schedules approved outreach, triages replies, and tracks deals. The goal is 28 new first emails per day; actual sends depend on quality and inbox capacity.

---

## Table of Contents

- [Overview & Architecture](#overview--architecture)
- [How leads and contacts are found](#how-leads-and-contacts-are-found)
- [Complete Feature Matrix](#complete-feature-matrix)
  - [1. Multi-Source Lead Prospecting](#1-multi-source-lead-prospecting)
  - [2. High-Confidence B2B Email Prospecting & Finder Engine](#2-high-confidence-b2b-email-prospecting--finder-engine)
  - [3. Deep AI Company Research & Qualification](#3-deep-ai-company-research--qualification)
  - [4. Hyper-Personalized Multi-Touch Drafting](#4-hyper-personalized-multi-touch-drafting)
  - [5. Deliverability, Safety Gates & Sending Control](#5-deliverability-safety-gates--sending-control)
  - [6. Multi-Provider Inboxes & Transport](#6-multi-provider-inboxes--transport)
  - [7. Inbound Reply Triage & Autonomous Alerting](#7-inbound-reply-triage--autonomous-alerting)
  - [8. Deal CRM & Multi-Currency Pipeline](#8-deal-crm--multi-currency-pipeline)
  - [9. Web Dashboard (Local & Vercel Cloud)](#9-web-dashboard-local--vercel-cloud)
  - [10. Hands-Free Background Daemon & Doctor](#10-hands-free-background-daemon--doctor)
- [Pipeline Workflow](#pipeline-workflow)
- [CLI Reference](#cli-reference)
- [Configuration & Settings](#configuration--settings)
- [Quickstart & Setup](#quickstart--setup)
- [Background schedule and Mac power](#background-schedule-and-mac-power)
- [Running Agent Outreach without keeping your computer online](#running-agent-outreach-without-keeping-your-computer-online)
- [Dashboard Deployment (Vercel + Turso)](#dashboard-deployment-vercel--turso)
- [Project Directory Structure](#project-directory-structure)
- [Deliberate Design & Compliance Boundaries](#deliberate-design--compliance-boundaries)
- [Testing](#testing)

---

## Overview & Architecture

`agent-outreach` is designed for solo founders, agencies, consultants, software engineers, and growth teams who want an enterprise-grade outbound acquisition system without monthly SaaS subscriptions.

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                         LEAD SOURCES                                             │
│  Remote Job Boards • YC Directory • Hacker News • UK Companies House • Dubai Land Reg • OSM     │
└────────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                 │
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                  B2B PROSPECTING & FINDER ENGINE                                  │
│  Domain Normalizer ➔ Public Discovery (Web/GitHub/RSS) ➔ Pattern Deduction ➔ Permutation Gen      │
│  ➔ Local Validation (RFC/DNS/MX/Catch-all) ➔ 0-100 Confidence Scoring ➔ Free-Tier API Fallback   │
└────────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                 │ (28 draft/day target; actual volume depends on sources, contacts and send capacity)
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                  DEEP RESEARCH & QUALIFICATION                                   │
│  Website Scrape (Home/About/Services/Team) + Google News RSS ➔ Gemini / Groq Brief + Fit Score    │
└────────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                 │ (Gated: Fit Score ≥ 6)
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    AI DRAFTING & ROTATION                                        │
│  Email 1 + Follow-up 1 (Day 4) + Follow-up 2 (Day 10) + LinkedIn Note • A/B Angles • Zero Hollows│
└────────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                 │
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 HUMAN-IN-THE-LOOP APPROVAL                                       │
│  CLI (`review`) or Web Dashboard ➔ Edit / Regenerate / Approve / Reject (Auto-approve threshold) │
└────────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                 │
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 SAFE SCHEDULED SENDING ENGINE                                    │
│  Pre-send Sync Assertion ➔ Durable State ➔ Market Windows (London/Dubai/IST) ➔ Jittered 8-15m   │
│  ➔ Zoho REST API / Gmail SMTP ➔ Warmup Ramp ➔ Bounce Rate Monitor                                │
└────────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                 │
                                                 ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                INBOUND REPLY & DEAL PIPELINE                                     │
│  IMAP / Zoho Inbound Sync ➔ Instant Sequence Halt ➔ Gemini Triage ➔ Telegram Ping ➔ 1-Page Plan │
│  ➔ Multi-Currency Deal CRM (USD / GBP / AED / INR)                                               │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## How leads and contacts are found

**What “Find & draft new leads” runs.** The `prepare` job runs the enabled entries in `prospecting` in `config/settings.yaml`, then visits each new company's website, checks its email address, tries to confirm named founder guesses, researches fit, and drafts up to the available capacity. It is scheduled for 07:30 Monday–Saturday in `sending.home_timezone` and can also be started in the Engine tab. Each source's `max_new` caps additions **per run**, not guaranteed finds. Existing email or company domain, suppressed addresses, and already processed posts are skipped. Failed or unreachable sources reduce output.

| Lead source in the current settings | Selection parameters |
|---|---|
| OpenStreetMap | Estate agents in Gulf and Indian cities; advertising/marketing agencies in UK and US cities. City lists, business tags, and `max_new` are set per job. |
| Hacker News and YC | Recent “Seeking freelancer” or “Who is hiring?” comments filtered by work terms, internship terms, remote eligibility and exclusions; YC hiring startups filtered by region, team size and batch year. |
| Remote boards and startup ATS | Remotive, Himalayas, RemoteOK, Jobicy, We Work Remotely and Working Nomads, plus known startups' Greenhouse/Lever/Ashby boards. Role, contract type, location, excluded terms and post age decide whether a job qualifies. |
| Launch HN, funding news, GitHub | Fresh launches; funding headlines matching AI/software terms; recently active repos with good-first-issue work. These supply a timely reason to contact a startup, not proof it wants an email. |
| Directories and registries | Firecrawl searches HubSpot/Webflow and Gulf property directories; Companies House searches UK SIC codes and locations. The DLD CSV and Apify Maps jobs are currently **disabled**. |

Global freshness caps are **7 days for job posts**, **14 days for news**, and **24 hours for community posts**; each source may be stricter. Undated job and community posts are skipped. The source rules also reject location restrictions, senior-only or unpaid work where configured, and posts saying AI-written applications are unwelcome. A company website guessed from a name must show a matching company name; the Companies House path also checks its company number. Source-specific filters and limits are editable in `config/settings.yaml`.

**Which address is used?** The automatic run uses an email published in a job/HN post or registry when present. Otherwise it reads the company's site and prefers a **named address on that company's domain** over a generic address such as `info@`; it does not assume the named mailbox belongs to the founder. If no company-domain address is published and a named founder is known, it may construct a possible `first@domain` or `first.last@domain` address. That is marked **guessed** and is not researched or drafted until the separate finder confirms it. With no usable address, the lead stops rather than entering the send queue. Community board posts are a separate manual-reply workflow and do not feed cold email.

**What “verified” means here.** The automatic `verify` step checks address format, disposable/placeholder/role names and MX records. An MX record says the *domain* receives mail; it does **not** prove a particular person's mailbox exists. Published addresses can therefore be `valid` or generic `risky` without mailbox-level verification. For a named guess, the Prospecting Desk/finder resolves the official domain, looks for the exact address on public pages/search/GitHub, learns company address patterns, checks MX and catch-all behavior, then scores evidence from 0–100. Below 75, it may query budgeted providers. Its “verified” (90+) and “high confidence” (75–89) tiers are evidence scores, not guaranteed delivery. Unconfirmed guesses stay parked. The finder is automatic for guessed named founders (up to 15 per `prepare` run) and available on demand in the Prospecting Desk; it does **not** run a provider lookup for every email found on a website or post.

**Current limits and send gates.** The daily target is **28 new, individually researched first emails**; follow-ups do not count toward this target. The seven segment quotas add up to 28 (6 Gulf real estate, 4 UK agencies, 2 US agencies, 4 international freelance, 4 international startup internships, 4 India real estate, 4 India startup internships). Prospecting Desk also targets 28 verified or high-confidence contacts. Research fit must be **8/10 or higher** before a first email can be drafted or sent. First emails also require a valid address from a public source or verified provider; risky role addresses and guesses are held. Auto-approval stays off, so every first email waits for your review. The single Zoho inbox has a separate **total-send** warm-up of 10 → 20 → 30 → 35 per day, including follow-ups. This means 28 new emails is a target, not a guaranteed daily send count: follow-ups and warm-up consume total capacity. The sender also checks suppression, placeholders, inbox freshness, recipient-market hours, an 8–15 minute gap, and the bounce threshold.

The current external lookup budgets are Firecrawl **60 credits/day** and, for the separate finder, Prospeo **5/day**, Skrapp **3/day**, Hunter **2/day**, each also subject to monthly and per-run caps. Skrapp's API key is currently absent, so it is skipped. A lookup credit can be spent without finding an address. See `provider_budget`, `firecrawl`, `targeting`, `sending`, `segments`, and `prospecting` in `config/settings.yaml` for the live parameters.

---

## Complete Feature Matrix

### 1. Multi-Source Lead Prospecting

The engine automatically collects high-intent leads across 8+ zero-cost public channels:

Only fresh postings are used: forum and Reddit posts from the last 24 hours, job posts from the last 7 days, news from the last 14 days (`freshness:` in settings). Undated posts are skipped. New source types and community boards from `settings.example.yaml` reach your own `settings.yaml` automatically (turn off with `sources_from_example: false`). Check them all live with `python -m outreach sources-check`.

- **Remote Job Boards**: Remotive, Himalayas, RemoteOK, Jobicy, We Work Remotely and Working Nomads: contract roles become freelance leads, intern/junior roles internship leads.
- **Startups' own job boards** (`ats`): Greenhouse, Lever and Ashby public APIs, checked for every startup already in your leads; a fresh intern / AI-engineer / contract role becomes the email's hook.
- **Funding news** (`funding`): Inc42, Entrackr, YourStory and TechCrunch RSS; startups that raised in the last two weeks.
- **GitHub good first issues** (`github`): startups' repos with open beginner tickets, for internship emails that mention a PR you opened.
- **Directory search** (`search`): HubSpot/Webflow partner agencies and Bayut/Property Finder agency pages via Firecrawl search (daily credit cap applies).
- **Y Combinator Startup Directory**: Scrapes YC companies filtering by batch, hiring status, and target industry.
- **Hacker News Algolia API**: Real-time parsing of monthly "Who is hiring?", "Seeking freelancer?", and "Launch HN" threads.
- **UK Companies House**: Queries the official Companies House API for newly incorporated agencies, SIC code classifications, and officer/director rosters.
- **Dubai Land Department Register**: Scrapes registered UAE real estate brokerages, active license numbers, and managing directors.
- **OpenStreetMap (Overpass API)**: Extracts local businesses across specific geographic bounding boxes (e.g. Dubai, London, Bangalore).
- **Meta Ad Library Hunter**: Generates curated search links for advertisers running click-to-WhatsApp and lead ads in target regions.
- **Community Forum Monitor**: the n8n, Bubble and Make forums and r/forhire, r/n8n, r/automation, r/zapier, r/nocode, r/AI_Agents, r/SaaS, r/hiring; flags fresh `[Hiring]` posts every 30 minutes, drafts a reply, and notifies via Telegram.
- **Custom CSV/JSON Importer**: Import your own lists with deduplication and segment assignment (`python -m outreach import leads.csv --segment uk_agencies`).

---

### 2. High-Confidence B2B Email Prospecting & Finder Engine

The separate Prospecting Desk finds and scores named professional addresses. Its **28/day** value is a search target; the automatic lead run invokes it only to confirm founder guesses.

- **Domain Normalization & Resolution**:
  - Cleans protocols, paths, and subdomains (`www.`, `blog.`, `docs.`).
  - Automatically identifies and filters out social network profiles (LinkedIn, X/Twitter, Instagram, Facebook, TikTok) and web directories.
  - Automatically infers official domains from company names using cache, local DB history, and web heuristics.
- **Public-Source Evidence Discovery**:
  - Deep-crawls official company pages (`/about`, `/team`, `/contact`, `/people`, `/leadership`, `/careers`).
  - Scrapes indexed search snippets and Google News RSS feeds for publicly exposed employee email addresses.
  - Inspects public GitHub repositories and commit author logs for `@company.com` domains.
- **Intelligent Pattern Deduction**:
  - Infers naming conventions from any discovered employee address across 10 pattern templates:
    - `first@domain.com`
    - `first.last@domain.com`
    - `firstlast@domain.com`
    - `first_last@domain.com`
    - `first-last@domain.com`
    - `f.last@domain.com`
    - `flast@domain.com`
    - `firstl@domain.com`
    - `last@domain.com`
    - `last.first@domain.com`
  - Calculates dominant pattern distribution and candidate confidence.
- **Local Validation Pipeline (Zero API Spend)**:
  - Basic address-format validation.
  - DNS & MX record lookup with mail provider signature recognition (Google Workspace, Microsoft 365, Zoho, ProtonMail).
  - Disposable domain detection.
  - Role-address detection (`info@`, `support@`, `sales@`, `admin@`, `contact@`, `jobs@`) preventing generic inboxes from being treated as decision-makers.
  - Heuristic catch-all server detection.
- **Granular 0-100 Confidence Scoring**:
  - `+45` Exact candidate email found publicly in indexed source
  - `+25` Matches confirmed company naming convention
  - `+10` Multiple discovered employee emails corroborate pattern
  - `+10` Active, valid MX mail servers confirmed
  - `+10` Localpart distinctly matches target prospect's full name
  - `-15` Catch-all domain penalty
  - `-20` Unverified pattern penalty
  - `-30` Generic role-address penalty
  - **Tiers**: `Verified (90-100%)`, `High Confidence (75-89%)`, `Needs Review (55-74%)`, `Unresolved (<55%)`. These are confidence labels, not mailbox delivery guarantees.
- **Sequential Free-Tier Fallback Dispatcher**:
  - Only triggered when local confidence is insufficient (<75%).
  - Tries configured providers in order: **Prospeo → Skrapp → Hunter**, with at most two charged lookups per person and the daily/monthly budgets in `config/settings.yaml`.
  - Prospeo requests verified email through its [person enrichment API](https://prospeo.io/api-docs/enrich-person); Hunter is the last fallback. Provider matches still pass the local sending checks.
  - Strictly caps and records monthly credit usage in SQLite (`provider_credits` table).
- **Comprehensive Provenance**:
  - Stores every candidate's evidence points, discovery URLs, MX provider, and scoring rationale in the database.

---

### 3. Deep AI Company Research & Qualification

- **Primary AI (Google Gemini)**: Leverages `gemini-2.0-flash` or `gemini-1.5-flash` for high-throughput, low-latency research and sequence generation.
- **Backup AI (Groq)**: Automatically falls back to Groq (`llama-3.3-70b-versatile`, `mixtral-8x7b-32768`) when Gemini free quotas are exhausted or rate-limited.
- **Context Synthesis**: Scrapes home, about, services, careers, and team pages; combines them with real-time Google News RSS headlines.
- **Structured Research Brief**: Outputs structured JSON containing company summary, estimated tech stack, key operational pain points, and specific relevant pitch angles.
- **Fit Scoring (0-10)**: Strict qualification gate—leads scoring below `6` are automatically marked `unfit` and pruned from sending pipelines.

---

### 4. Hyper-Personalized Multi-Touch Drafting

- **Multi-Touch Sequences**:
  - **Email 1**: Compelling hook based on research brief, specific problem articulation, concise proof point, and frictionless CTA (free one-page plan, no call required).
  - **Follow-Up 1 (Day +4)**: Threaded follow-up highlighting a concrete implementation example or case study.
  - **Follow-Up 2 (Day +10)**: Threaded low-friction close-out / breakup email.
  - **LinkedIn Note / InMail**: Pre-drafted 300-character personalized connection note and direct message.
- **Dynamic A/B Angle Testing**:
  - Each segment defines multiple messaging angles (e.g. *Speed to Lead* vs *Attribution & Tracking*).
  - Automatically balances angles across leads and tracks reply rates per angle.
- **Quality & Hallucination Defense**:
  - Automatic placeholder detection rejects any draft containing `[Name]`, `[Company]`, `{{...}}`, or hollow templates.
  - India market safety cap: prevents more than 25-33% of daily sending volume from going to domestic markets.

---

### 5. Deliverability, Safety Gates & Sending Control

- **Replies first**: the background inbox job reads replies every 10 minutes, and nothing sends from an inbox that hasn't been read in the last 30 minutes, so a reply always stops its sequence before the next email.
- **Durable `sending` State**: the email is marked `sending` before the provider call, so a crash can't send it twice. If the mail provider refuses it (a 4xx answer, or a login problem), it simply waits and retries; after 3 refusals it's held. If the connection drops mid-send, it may have gone out: it appears under **Review → Needs a decision** with "It's in my Sent folder" / "Not sent, try again", and Telegram tells you.
- **Safety checks before every send**, shown on the email under **Review → Held** with the reason:
  - never: suppressed addresses, empty text, template placeholders, invalid/guessed addresses, companies whose post or site rejects AI-written applications;
 - **Held by the first-email quality gate:** research fit below 8/10, risky role addresses, or addresses without public or provider-verified evidence. Follow-ups remain separate.
  - for auto-approved template emails only: generic `info@` addresses and AI confidence under 85%. Your own approval in Review is enough otherwise.
- **Human-in-the-Loop Review**:
  - Interactive CLI review mode (`python -m outreach review`).
  - Web dashboard draft editor with instant AI regeneration.
  - Optional auto-approval threshold (`--min-confidence 0.85`).
- **Pacing & Warm-Up Ramp**:
  - Maximum 2 emails dispatched per tick with randomized 8-15 minute gaps.
  - Multi-week warmup schedule (e.g. Week 1: 10/day ➔ Week 2: 20/day ➔ Week 3: 30/day ➔ Week 4+: 35/day).
- **Timezone-Aware Delivery Windows**:
  - Delivers only within each market's verified business hours (e.g. UK: Mon-Wed 09:00-16:00 London time; Gulf: Mon-Fri 09:30-17:30 Dubai time).
- **Automated Bounce Circuit Breaker**:
  - Tracks bounce rates over a rolling 7-day window. Automatically pauses an inbox if bounce rate exceeds 3.0%.

---

### 6. Multi-Provider Inboxes & Transport

- **Gmail (`smtp`)**: Direct TLS/SSL dispatch with Google App Passwords; IMAP reply tracking; drafts saved to `[Gmail]/Drafts`.
- **Zoho Mail REST API (`zoho_api`)**: Sends directly through Zoho's official REST API using Self-Client OAuth tokens. **Allows automated sending on Zoho's free plan** without requiring paid SMTP/IMAP addons.
- **Zoho Paid (`smtp`)**: Works with standard `smtp.zoho.in:465` and `imap.zoho.in`.
- **RFC-Compliant Threading**: Preserves `Message-ID`, `In-Reply-To`, and `References` headers so follow-ups appear in the same thread.

---

### 7. Inbound Reply Triage & Autonomous Alerting

- **Background Sync**: Checks inboxes every 10 minutes.
- **Automatic Sequence Kill**: Immediately halts all pending follow-ups when any reply (other than out-of-office) is received.
- **AI Classification**: Categorizes incoming messages:
  - `interested`: Prospect wants details, pricing, or a call.
  - `not_interested`: Explicit decline.
  - `out_of_office`: Auto-responder (reschedules follow-up).
  - `bounce`: Delivery failure (triggers sender reputation tracking).
  - `referral`: Prospect provided an alternative contact.
  - `unsubscribe`: Explicit opt-out request.
- **Opt-Out Suppression**: Automatically adds unsubscriptions to the local suppression database.
- **Instant Telegram Pings**: Sends an immediate alert to your Telegram bot with lead details, reply text, and a pre-drafted response.
- **One-Click Reply Dispatch**: Edit and send drafted replies directly from the Web Dashboard.

---

### 8. Deal CRM & Multi-Currency Pipeline

- **Deal Lifecycle**: Move opportunities through visual stages: `lead` ➔ `contacted` ➔ `call_booked` ➔ `proposal_sent` ➔ `won` / `lost`.
- **Multi-Currency Support**: Tracks revenue and proposals natively in **USD**, **GBP**, **AED**, and **INR**.
- **Automated "Make 1-Page Plan"**: One click generates a tailored, fixed-price project proposal synthesizing the lead's research brief and their specific reply.
- **Weekly Executive Digest**: Telegram digest delivered Monday morning with funnel conversion metrics, positive replies, and pipeline value.
- **Weekly LinkedIn posts on AI and automation**: every Monday, a take on one of this week's AI news stories (Hacker News + Google News, with the link), an opinion on the AI industry argued from the `linkedin.opinions` you write in `profile.yaml`, and a lesson from building AI systems. Opinions the AI adds are flagged "check before posting"; Caudal AI's work is never described.
- **Included Case Study Page**: Ready-to-deploy static case-study showcase (`site/index.html`).

---

### 9. Web Dashboard (Local & Vercel Cloud)

- **Dual Deployment Options**:
  - **Local**: `python -m outreach dashboard` serves locally on `http://127.0.0.1:7347`.
  - **Cloud**: Zero-maintenance serverless deployment on Vercel backed by a Turso edge database.
- **Security**: Password protected, secure HTTP-only session cookies, no API keys exposed to the client.
- **Key Desk Tabs**:
  - **Review**: Review drafted email sequences, inspect fit scores, edit copy, regenerate with custom instructions, approve, or reject.
  - **Prospecting Desk**: Dedicated UI for single prospect lookup, bulk CSV import, 28/day verified target meter, provider credit monitor, and full evidence audit modal.
  - **Respond / Inbox**: Unified inbox showing classified replies, AI drafted responses, and one-click sending.
  - **Deals**: Interactive CRM board for active negotiations with next-action dates.
  - **Engine**: Background daemon heartbeat, manual job runners, job logs, per-source lead discovery status, manual Reddit links, Ad Library search cards, and LinkedIn outreach tasks.
  - **Queue**: Dashboard actions waiting for the Mac, jobs currently running, and the recurring schedule.
  - **Today**: Replies, drafts, queued actions, and lead source failures that need attention.
  - **Results**: Real-time conversion funnels, A/B angle comparison, and source channel attribution.

---

### 10. Hands-Free Background Daemon & Doctor

- **Native macOS `launchd` / Linux `cron`**: `python -m outreach install` installs a launch agent that ticks every 5 minutes while the Mac is awake and the user is logged in. On Linux, `install` prints a crontab entry to add manually.
- **Autonomous Tick Workflow**:
  1. Synchronizes actions taken on the dashboard.
  2. Starts the inbox sync job every 10 minutes to read and triage replies.
  3. Sends due emails within market business hours.
  4. Runs morning preparation (`prepare`) at 07:30 Mon-Sat.
  5. Pushes updated snapshots to Turso / Dashboard.
- **`doctor` Diagnostics**: Shows the last engine tick, running jobs, launch agent status, and recent engine log lines.

## Background schedule and Mac power

Times below use `sending.home_timezone` in `config/settings.yaml` (currently `Asia/Kolkata`). The engine checks what is due on each 5-minute tick, so starts may be a few minutes after the listed time. Dashboard actions are queued in Turso; the local dashboard watchdog notices them about every 20 seconds while it is running. **Run all three** queues one action that starts `prepare`, `community`, and `content` as separate jobs. A job already running is skipped, while the others still start. The Queue tab shows waiting actions and running jobs; the Engine tab shows each job's outcome and log.

| Work | When | Result |
|---|---|---|
| Engine, approved-email sending, dashboard action pull and snapshot push | Every 5 minutes | Send only when all safety gates and the recipient market's business window allow it. |
| Inbox sync | Every 10 minutes | Stops follow-ups on replies and drafts answers. |
| Community boards | Every 30 minutes | Checks fresh posts, drafts replies, and alerts you; you reply manually. |
| Find and draft new leads | 07:30 Monday–Saturday | Prospect, enrich, verify, research, draft; review the drafts before sending. |
| LinkedIn posts | 08:00 Monday | Drafts three posts; review and publish them manually. |
| Weekly digest | 09:00 Monday | Sends the summary via Telegram when configured. |
| Ad Library and LinkedIn outreach tasks | 09:45 daily | Prepares links and tasks for manual work. |

The engine and credentials live on your Mac. **Keep it on, awake, connected to the internet, and logged in** for on-time runs. The hosted dashboard can stay open while the Mac sleeps, but actions wait in its queue; `launchd` cannot execute the Python jobs while the Mac is asleep or powered off. On wake, the next tick runs overdue daily work once and resumes recurring checks. Freshness limits still apply, so a late community run may miss posts older than 24 hours.

`caffeinate -i` in a Terminal window prevents *idle* system sleep while that command stays open. It can keep the installed launch agents ticking with the display asleep, but it does not power on a shut-down Mac, preserve connectivity, or reliably keep a laptop running with the lid closed. Keep the lid open and use AC power for unattended operation. `caffeinate` does not replace `python -m outreach install`; run `python -m outreach doctor` to check the agents and the last tick. For continuous operation independent of the Mac, run the local engine on an always-on host with the same configuration and secrets.

The three manual jobs need working source sites and internet access. Lead and LinkedIn drafting need a configured Gemini key (Groq is a fallback). Reddit requires approved Data API access for this external script. Request it through [Reddit's Data API form](https://support.reddithelp.com/hc/en-us/requests/new?ticket_form_id=14868593862164), describing the commercial lead-monitoring use honestly. Approval and free access are not guaranteed. Once approved, set `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, and a descriptive `REDDIT_USER_AGENT` in `.env`. Until then, check Reddit manually and let the engine monitor the n8n, Bubble, and Make forums. The engine does not scrape Reddit or retry anonymous RSS. Check each job's error and log in the Engine tab; a completed job does not mean every external source succeeded.

---

## Running Agent Outreach without keeping your computer online

You do not need to keep your Mac awake, plugged in, or connected to the internet for `agent-outreach` to run. The system can execute scheduled prospecting, reply synchronization, opportunity monitoring, follow-up processing, and approved email sending completely autonomously on GitHub Actions runners using persistent remote storage on Turso.

Local development with SQLite remains fully supported and untouched.

### GitHub Actions Architecture

The cloud execution model consists of four focused workflows under `.github/workflows/`:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                GITHUB ACTIONS WORKFLOWS                                │
├──────────────────────────┬──────────────────────────┬──────────────────────────────────┤
│ scheduled-monitoring.yml │ scheduled-outreach.yml   │ daily-maintenance.yml            │
│ (Every 30 min)           │ (Hourly: 0 * * * *)      │ (Daily: 03:00 UTC / 08:30 IST)   │
│                          │                          │                                  │
│ • Pull dashboard actions │ • Pull dashboard actions │ • Pull dashboard actions         │
│ • Sync inbound replies   │ • Verify fresh inbox     │ • Sync provider balances         │
│ • Screen Reddit/forums   │ • Prepare leads (4 hr)   │ • Generate AdLib/LinkedIn tasks  │
│ • Push dashboard snap    │ • Send approved outreach │ • Funnel analytics & reporting   │
│                          │ • Push dashboard snap    │ • Push dashboard snap            │
└──────────────────────────┴──────────────────────────┴──────────────────────────────────┘
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │   PERSISTENT TURSO DB     │
                        │  (libSQL Cloud Database)  │
                        └───────────────────────────┘
```

#### Strict Safety, Idempotency & Concurrency Guarantees
1. **Concurrency Protection**: `scheduled-outreach.yml` uses:
   ```yaml
   concurrency:
     group: outreach-sending
     cancel-in-progress: false
   ```
   This guarantees that send jobs will never run concurrently or overlap.
2. **Deterministic Sequence Identity**: Every message in a sequence has a deterministic unique identity (`idempotency_key = lead:{lead_id}:step:{step}`) backed by a `UNIQUE(lead_id, step)` constraint. The same sequence step can never be sent twice.
3. **Atomic Sending Transition**: Messages transition safely through `approved` ➔ `sending` (committed) ➔ `sent`. Before calling the mail provider, the message is claimed atomically with `UPDATE messages SET status='sending', sending_at=... WHERE id=? AND status='approved'`. If another worker or process already claimed it, `claimed.rowcount == 0` and the message is skipped.
4. **Stale Sending Recovery**: If a GitHub Actions runner crashes or is abruptly killed while an email is in-flight, subsequent runs automatically run `reconcile_stale_sending()`. Any message in `status='sending'` older than 15 minutes is safely transitioned to `status='needs_reconciliation'` so it will **never be blindly resent**.
5. **Human Approval Gate**: First-touch emails (`step == 0`) strictly require human approval (`status='approved'`) via the web dashboard or CLI before they can be sent.
6. **Isolated CI**: `ci.yml` runs on push and pull requests with `OUTREACH_ENV=local`. It executes tests in an isolated, offline environment and never touches production data or sends real emails.

---

### Schedules & Cadence

| Workflow | Trigger / Cadence | Commands Executed | Purpose |
|---|---|---|---|
| `scheduled-monitoring.yml` | `*/30 * * * *` (Every 30 min) | `python -m outreach dashboard-sync`<br>`python -m outreach sync`<br>`python -m outreach community` | Reads inbound replies, stops sequences on reply, alerts on hot leads via Telegram, and screens community `[Hiring]` posts. |
| `scheduled-outreach.yml` | `0 * * * *` (Every hour) | `python -m outreach dashboard-sync`<br>`python -m outreach sync`<br>`python -m outreach prepare` (every 4h)<br>`python -m outreach send --max 5` | Pulls approvals, ensures inbox is fresh, runs lead preparation if capacity allows, and trickles out approved follow-ups and outreach. |
| `daily-maintenance.yml` | `0 3 * * *` (Daily 03:00 UTC) | `python -m outreach dashboard-sync`<br>`python -m outreach providers-check`<br>`python -m outreach adlib`<br>`python -m outreach linkedin`<br>`python -m outreach report`<br>`python -m outreach digest` (Mondays) | Balances check, daily search and social task generation, pipeline funnel reporting, and weekly digest. |
| `ci.yml` | `push`, `pull_request` | `python -m compileall outreach tests`<br>`python -m pytest -q` | Fast regression and syntax validation on Python 3.12 without external network calls. |

All scheduled workflows support `workflow_dispatch` for manual triggering from the GitHub interface.

---

### Remote Database (Turso libSQL)

GitHub runners are ephemeral. To ensure complete state persistence across separate workflow executions, production execution uses Turso (hosted libSQL/SQLite).

#### Persistent Production State
When running in production, all state is durably stored in Turso:
- Prospects and email finder results (`prospects`, `prospect_domain_cache`, `provider_credits`)
- Contacts, research briefs, fit scores, and source provenance (`leads`)
- Sequences, multi-touch drafts, approval status, and send records (`messages`)
- Inbound replies, sentiment categories, and bounce records (`replies`)
- Delivery metrics, daily send volumes, and ramp tracking (`send_log`)
- Suppression and unsubscribe lists (`suppression`)
- Source checkpoints, job states, and sync cursors (`prospect_state`)
- Community opportunity board posts and drafts (`posts`)
- Deal CRM tracking, stage transitions, and values (`leads`)

#### Environment Distinction
- **`OUTREACH_ENV=production`**: All database calls connect directly to Turso via the native HTTP pipeline client (`outreach/turso.py`), sharing state between GitHub Actions and your Vercel dashboard.
- **`OUTREACH_ENV=local`**: Defaults to your local SQLite database at `data/outreach.db`.

#### Migrating Local Database to Turso
If you have existing leads, drafts, or suppression lists in your local `data/outreach.db`, migrate them to Turso with a single command:
```bash
python -m outreach migrate-to-turso
```

---

### Required GitHub Secrets

To run the workflows in GitHub Actions, navigate to **Settings ➔ Secrets and variables ➔ Actions** in your repository and configure the following secrets:

| Secret Name | Required? | Description |
|---|---|---|
| `TURSO_DATABASE_URL` | **Yes** | Your Turso database URL (e.g., `libsql://your-db.turso.io` or `https://...`). |
| `TURSO_AUTH_TOKEN` | **Yes** | Authentication token generated from your Turso dashboard or CLI. |
| `GEMINI_API_KEY` | **Yes** | Google Gemini API key (free at `aistudio.google.com/apikey`). |
| `GROQ_API_KEY` | Optional | Backup AI key for when Gemini is rate-limited (`console.groq.com`). |
| `ZOHO_CLIENT_ID` | Conditional | Zoho Self Client ID (if using Zoho Mail API). |
| `ZOHO_CLIENT_SECRET` | Conditional | Zoho Self Client Secret (if using Zoho Mail API). |
| `ZOHO_REFRESH_TOKEN` | Conditional | Zoho OAuth refresh token (obtain via `python -m outreach zoho-token <code>`). |
| `ZOHO_APP_PASSWORD` | Conditional | Zoho SMTP app password (for paid Zoho plans with SMTP enabled). |
| `GMAIL_APP_PASSWORD` | Conditional | Google App Password (if sending through Gmail SMTP). |
| `TELEGRAM_BOT_TOKEN` | Optional | Telegram bot token from `@BotFather` for mobile notifications. |
| `TELEGRAM_CHAT_ID` | Optional | Telegram chat ID (obtain via `python -m outreach telegram-setup`). |
| `REDDIT_CLIENT_ID` | Optional | Reddit Data API client ID (for Reddit lead monitoring). |
| `REDDIT_CLIENT_SECRET` | Optional | Reddit Data API client secret. |
| `REDDIT_USER_AGENT` | Optional | Descriptive user agent format: `platform:app-name:v1.0 (by /u/username)`. |
| `FIRECRAWL_API_KEY` | Optional | Firecrawl key for advanced web extraction (`firecrawl.dev`). |
| `PROSPEO_API_KEY` | Optional | Prospeo email finder API key. |
| `HUNTER_API_KEY` | Optional | Hunter.io email finder API key. |
| `SKRAPP_API_KEY` | Optional | Skrapp.io email finder API key. |
| `SETTINGS_YAML` | Optional | Raw content of `config/settings.yaml` (overrides default configuration). |
| `PROFILE_YAML` | Optional | Raw content of `config/profile.yaml` (overrides default profile). |

---

### Manually Triggering Jobs (`workflow_dispatch`)

You can run any job on demand directly from GitHub without waiting for the next cron schedule:
1. Navigate to the **Actions** tab in your GitHub repository.
2. In the left sidebar, click the workflow you want to run (e.g., **Scheduled Outreach**).
3. Click the **Run workflow** dropdown on the right.
4. (Optional) Provide input parameters:
   - **`max_sends`**: Adjust how many emails to send in this run (e.g., `2` or `5`).
   - **`dry_run`**: Check the box to test without dispatching real emails.
   - **`run_prospecting`**: Check the box to immediately trigger discovery and sequence drafting.
5. Click **Run workflow**.

---

### Disabling Jobs

If you ever want to temporarily pause autonomous cloud runs:
- **Disable a single workflow**: In the **Actions** tab, select the workflow in the left sidebar, click the `...` (options) menu next to the workflow name, and select **Disable workflow**.
- **Pause outreach sending globally**: You do not need to disable GitHub Actions. In the web dashboard, click **Pause sending** (or run `python -m outreach pause`). The workflows will continue to sync replies and monitor opportunities, but the sender will hold all emails until resumed.

---

### Viewing Logs & Diagnostics

- **GitHub Run Logs**: In the **Actions** tab, click on any completed or in-progress run. Expand individual steps (e.g., `Send approved outreach & due follow-ups` or `Sync replies & triage inbox`) to view exact CLI outputs, sender status, and error traces.
- **Remote Dashboard**: View live queue status, review holds, and engine heartbeat in the web desk hosted on Vercel.

---

### Running Locally

Local execution is completely preserved:
- To run with local SQLite: simply run CLI commands without `OUTREACH_ENV=production`. Everything operates against `data/outreach.db`.
- To run locally against the remote Turso database:
  ```bash
  export OUTREACH_ENV=production
  python -m outreach send
  ```

---

### Safe Dry-Run Mode

You can verify the entire sending and delivery pipeline without sending a single real email:
- **In GitHub Actions**: Trigger `Scheduled Outreach` manually with the `dry_run` checkbox enabled.
- **In Local CLI**:
  ```bash
  python -m outreach send --dry-run
  ```
  The runner evaluates all candidates, checks time windows, inspects templates, and logs exactly what would be sent without contacting the email provider.

---

## Pipeline Workflow

| Step | Command | Typical Frequency | Purpose |
|---|---|---|---|
| **Prospect** | `prospect` | Cron (in `prepare`) | Scrapes public directories, job boards, HN, UK/Dubai registers, OSM. |
| **B2B Finder** | `prospect-find` / `prospect-enrich` | On-demand / Dashboard | Discovers and validates emails; scores confidence 0-100; enforces 28/day target. |
| **Import** | `import <file.csv>` | On-demand | Bulk imports existing lists into a target segment. |
| **Enrich** | `enrich` | Daily | Scrapes website signals, team pages, and published company contacts. |
| **Verify** | `verify` | Daily | Validates syntax, DNS/MX records, and checks disposable/role addresses. |
| **Research** | `research` | Daily | Gemini research brief + Google News synthesis; computes 0-10 fit score. |
| **Draft** | `draft` | Daily | Drafts Email 1, two follow-ups, and LinkedIn note with A/B angles. |
| **Review** | `review` / `approve` | Daily (15-20 min) | Human review via terminal or dashboard (edit, approve, regenerate). |
| **Send** | `send` | Every 5 min | Safe scheduled delivery inside timezone windows with randomized gaps. |
| **Sync** | `sync` | Every 10 min | Fetches inbound replies, stops sequences, and triggers AI reply triage. |
| **Reply** | `reply <id>` | On-demand | Dispatches approved reply to interested prospect. |
| **Ad Library** | `adlib` | Daily | Generates targeted Meta Ad Library search queries. |
| **Community** | `community` | Every 30 min | Monitors n8n & Reddit for `[Hiring]` posts; drafts proposals. |
| **Content** | `content` | Mondays | Generates 3 weekly LinkedIn thought-leadership drafts. |
| **Digest** | `digest` | Mondays | Sends weekly KPI summary and pipeline status to Telegram. |
| **Report** | `report` | On-demand | Prints terminal funnel report per segment and source. |

---

## CLI Reference

### Prospecting & B2B Email Finder

```bash
# Find and verify a single prospect (positional or named flags)
python -m outreach prospect-find "Patrick" "Collison" "Stripe" --domain stripe.com --title "CEO"
python -m outreach prospect-find --first "Patrick" --last "Collison" --company "Stripe"

# Bulk enrich prospects from CSV with a verified target cap and multi-threading
python -m outreach prospect-enrich leads.csv --target 28 --workers 3 --output results.csv

# View prospecting database metrics, verification tiers, and provider credits
python -m outreach prospect-stats

# Export prospects by status (verified, high_confidence, uncertain, all)
python -m outreach prospect-export verified_prospects.csv --status verified
```

### Daily Workflow & Execution

```bash
# Initialize SQLite database and tables
python -m outreach init

# Run full morning pipeline (Prospect -> Enrich -> Verify -> Research -> Draft)
python -m outreach prepare
python -m outreach prepare --mock            # Dry run on mock.db (no real API calls)
python -m outreach prepare --skip-prospect   # Process existing leads only

# Scrape leads from all sources or a specific source
python -m outreach prospect
python -m outreach prospect --source jobs    # choices: jobs, yc, hn, companies_house, dubai, osm

# Import custom CSV
python -m outreach import my_leads.csv --segment uk_agencies

# Run individual pipeline stages
python -m outreach enrich --limit 150
python -m outreach verify
python -m outreach research --limit 45
python -m outreach draft --limit 28

# Review and approve drafts
python -m outreach review                    # Interactive terminal review
python -m outreach approve --min-confidence 0.85 # Bulk approve high-confidence drafts

# Send scheduled emails
python -m outreach send                      # Sends next due batch (2 emails)
python -m outreach send --dry-run            # Previews eligible sends without sending

# Check inboxes, triage replies, and suppress opt-outs
python -m outreach sync

# Send reply to prospect
python -m outreach reply 42                  # Edit and send response to reply #42
python -m outreach done 42                   # Mark reply as handled without sending

# Growth & social workflows
python -m outreach adlib                     # Generate Meta Ad Library searches
python -m outreach linkedin                  # Output hand-sent LinkedIn tasks
python -m outreach community                 # Check n8n / Reddit hiring boards
python -m outreach post-done 15              # Mark community post #15 as answered
python -m outreach content                   # Generate 3 weekly LinkedIn drafts
python -m outreach digest                    # Push weekly digest to Telegram
python -m outreach report                    # Display terminal funnel analytics
```

### System Administration & Diagnostics

```bash
# Emergency controls
python -m outreach pause                     # Pause all outbound sending
python -m outreach resume                    # Resume outbound sending
python -m outreach suppress prospect@domain.com  # Add email or @domain to suppression list

# Health & testing
python -m outreach doctor                    # Run full system diagnostics
python -m outreach groq-check                # Test Groq backup LLM connectivity
python -m outreach sources-check             # Fetch every lead source once, show fresh counts (saves nothing)
python -m outreach zoho-check --probe        # End-to-end Zoho API & reply probe
python -m outreach zoho-token <code>         # Exchange Zoho Self-Client code for refresh token
python -m outreach telegram-setup            # Pair Telegram bot and find chat ID

# Background daemon & dashboard
python -m outreach install                   # Install background daemon (launchd/cron)
python -m outreach uninstall                 # Remove background daemon
python -m outreach tick                      # Execute one 5-minute background tick
python -m outreach dashboard --port 7347     # Run web dashboard locally
python -m outreach dashboard-sync            # Sync local actions with Turso database
```

---

## Configuration & Settings

### 1. Environment Variables (`.env`)

Copy `.env.example` to `.env` and configure keys as needed:

```bash
# Primary AI
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-2.0-flash
GEMINI_RESEARCH_MODEL=gemini-2.0-flash
GEMINI_REPLY_MODEL=gemini-2.0-flash

# Backup AI (Optional, free at console.groq.com)
GROQ_API_KEY=your_groq_api_key

# Web Scraping & Domain Resolution (Optional fallback)
FIRECRAWL_API_KEY=your_firecrawl_key

# GitHub good-first-issue source (optional, free: raises the limit from 60 to 5,000 requests/hour)
GITHUB_TOKEN=your_github_token

# Fallback Email APIs (Optional free tiers)
PROSPEO_API_KEY=your_prospeo_key             # free plan; the engine uses at most 80% of it
HUNTER_API_KEY=your_hunter_key               # free plan (25 searches/month); at most 80%, ~2 a day
SKRAPP_API_KEY=your_skrapp_key               # free plan; at most 80%
# Provider credit guard rails (settings.yaml -> provider_budget): every call counts, found or not; never the
# same person twice; monthly cap = use_pct of plan_monthly, spread evenly per day; per-bulk-run cap; a provider
# pauses itself on a rejected key (401/403), empty account (402) or rate limit (429). Check with:
#   python -m outreach providers-check        (real balances via free account endpoints + this month's budget)

# Zoho Mail API Sending (Free plan compatible)
ZOHO_CLIENT_ID=your_zoho_client_id
ZOHO_CLIENT_SECRET=your_zoho_client_secret
ZOHO_REFRESH_TOKEN=your_zoho_refresh_token

# Gmail SMTP Sending (Alternative)
GMAIL_APP_PASSWORD=your_gmail_app_password

# Telegram Alerts
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id

# Cloud Dashboard Sync (Turso + Vercel)
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your_turso_token
DASHBOARD_PASSWORD=your_secure_password
SESSION_SECRET=your_session_secret
```

### 2. Segment & Sending Rules (`config/settings.yaml`)

Define target markets, sending windows, offers, pricing, and A/B angles:

```yaml
targeting:
  india_share_max: 0.33       # Maximum domestic market share
  min_fit: 8                  # Minimum research fit score (0-10)

sending:
  home_timezone: Asia/Kolkata
  # allowed_segments: [gulf_realestate, intl_freelance_posts]   # optional: only these segments send
  min_gap_minutes: 8
  gap_jitter_minutes: 7
  max_bounce_rate: 0.03       # Auto-pause threshold (3%)
  daily_first_target: 28 # New first emails; follow-ups excluded
  ramp_by_week: [10, 20, 30, 35]

inboxes:
  - email: founder@yourdomain.com
    display_name: Your Name
    transport: zoho_api       # or 'smtp'
    zoho_dc: in               # 'in', 'com', 'eu'
    max_per_day: 35
    warmup_start: 2026-10-01

segments:
  uk_agencies:
    daily_new: 4
    market: uk
    timezone: Europe/London
    send_days: [1, 2, 3, 4]   # Mon-Thu
    send_windows: ["09:00-11:30", "13:30-16:00"]
    offer: "Custom AI Lead Scraping & Enrichment System"
    price: "GBP 500 fixed, paid on delivery"
    angles:
      - id: speed
        focus: "Rapid turnaround and bespoke workflow integration"
      - id: quality
        focus: "Zero-bounce verified prospect data"
```

### 3. Personal Profile (`config/profile.yaml`)

Store your professional background, case studies, portfolio links, and signatures for AI grounding:

```yaml
name: Your Name
title: AI Automation & Full-Stack Engineer
bio: "I build bespoke AI agents, web scrapers, and outreach automation engines."
portfolio_url: https://yourportfolio.com
case_studies:
  - client: "Fintech Agency"
    result: "Automated 2,000 monthly lead enrichments with 98% email deliverability."
signatures:
  freelance: |
    Best regards,
    Your Name
    https://yourportfolio.com
```

---

## Quickstart & Setup

Follow these commands to install and test the system on macOS or Linux:

```bash
# 1. Clone repository and set up virtual environment
git clone https://github.com/AvnishRana25/agent-outreach.git
cd agent-outreach
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment and profiles
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
cp config/profile.example.yaml config/profile.yaml

# 4. Initialize database and verify tests
python -m outreach init
python -m pytest -q

# 5. Check email & messaging integrations
python -m outreach zoho-check --probe
python -m outreach telegram-setup

# 6. Test a single prospect lookup
python -m outreach prospect-find "Patrick" "Collison" "Stripe" --domain stripe.com

# 7. Launch local dashboard desk
python -m outreach dashboard --port 7347
```

---

## Dashboard Deployment (Vercel + Turso)

Run the full web desk on a secure, free Vercel deployment:

1. **Create Turso Database (Free)**:
   - Create a database at [turso.tech](https://turso.tech).
   - Copy `libsql://...` database URL and authentication token into `.env`.
   - Run `python -m outreach dashboard-sync` to initialize remote tables.
2. **Deploy to Vercel**:
   - Import the repository in Vercel.
   - **Root Directory**: `dashboard`.
   - **Framework Preset**: Other.
   - Add Environment Variables:
     - `TURSO_DATABASE_URL`
     - `TURSO_AUTH_TOKEN`
     - `DASHBOARD_PASSWORD`
     - `SESSION_SECRET`
   - Click **Deploy**.
3. **Automate Sync**:
   - Run `python -m outreach install` on your machine.
   - The engine automatically applies your dashboard actions and pushes fresh snapshots every 5 minutes.

---

## Project Directory Structure

```
agent-outreach/
├── config/
│   ├── profile.example.yaml      # Bio, proof points, signatures
│   └── settings.example.yaml     # Inboxes, segments, schedules, offers
├── dashboard/
│   ├── api/index.py              # Serverless Python backend (Auth, Turso sync, API)
│   └── index.html                # Single-page Desk UI (Review, Prospecting, Deals, Engine)
├── data/                         # Local databases, CSV outputs, and markdown tasks
├── prompts/
│   ├── draft_system.md           # Email sequence & LinkedIn drafting instructions
│   ├── reply_system.md           # Reply triage and classification prompt
│   └── research_system.md        # Company qualification and brief generation
├── outreach/
│   ├── cli.py                    # Unified CLI command router
│   ├── community.py              # Forum & Reddit [Hiring] monitor
│   ├── config.py                 # Configuration loader and validation
│   ├── db.py                     # SQLite / Turso remote database schema and migrations
│   ├── eligibility.py            # Central send eligibility gatekeeper (Gates A-J)
│   ├── engine.py                 # Background daemon scheduler & tick loop
│   ├── enrich.py                 # Website crawler and published email extractor
│   ├── evidence.py               # Evidence extraction and personalization validation
│   ├── firecrawl.py              # Optional Firecrawl fallback client
│   ├── groq.py                   # Backup Groq LLM client
│   ├── growth.py                 # 1-page proposal and deal stage tracking
│   ├── importer.py               # CSV and list importer
│   ├── llm.py                    # Gemini client with retry & fallback logic
│   ├── personalize.py            # AI sequence generation (mode-specific copywriting)
│   ├── prospect.py               # Directory and job board lead scrapers
│   ├── replies.py                # IMAP/Zoho reply fetcher & classifier
│   ├── report.py                 # Segment funnel reporting
│   ├── research.py               # Company research and fit scoring
│   ├── review.py                 # Interactive terminal review interface
│   ├── scoring.py                # Mode-aware opportunity scoring & intent detection
│   ├── sender.py                 # Safe scheduled send executor & 28/day allocation
│   ├── sources.py                # Public data source drivers
│   ├── transport.py              # SMTP, IMAP, and Zoho REST API transports
│   ├── turso.py                  # Turso edge database client
│   ├── verification.py           # Multi-method email verification & deliverability scoring
│   ├── website.py                # Domain resolution and discovery
│   ├── analytics.py              # Phase 3 outcome analytics & learning safety
│   └── prospecting/              # Free B2B Email Prospecting Engine
│       ├── config.py             # Weights, thresholds, and provider credit caps
│       ├── models.py             # Pydantic data schemas
│       ├── domain/               # Normalization and domain resolution
│       ├── validation/           # RFC syntax, DNS/MX, role, catch-all
│       ├── email/                # Pattern deduction, permutations, scoring
│       ├── discovery/            # Site crawler, search, GitHub scanner
│       ├── providers/            # Prospeo, Hunter, Skrapp fallback clients
│       └── pipeline/             # Single and bulk multi-threaded processors
├── tests/                        # 198 comprehensive automated unit and integration tests
├── PLAYBOOK.md                   # Strategic market research & 30-day outreach playbook
├── GROWTH.md                     # High-ticket closing guide & conversion channels
└── requirements.txt              # Production Python dependencies
```

---

## Deliberate Design & Compliance Boundaries

- **Zero Guessed Emails**: The pipeline prioritizes publicly exposed addresses and confirmed patterns. Unverified emails are scored down and flagged for manual review.
- **Anti-Spam & Deliverability Standards**: Staggered sending gaps (8-15 mins), strict 28/day ceiling, automated bounce shutoffs, and immediate opt-out suppression keep sender domain reputation pristine.
- **Mode-Specific Funnels**: Distinct scoring, targeting, and copywriting strategies for FREELANCE vs INTERNSHIP opportunities.
- **Strategic Daily 28 Allocation**: Configurable split (12 Freelance, 8 Internship, 8 Follow-ups) with optional quota borrowing and strict quality enforcement.
- **Learning & Optimization Safety**: The analytics engine observes real conversions and proposes recommendations with explicit human approval required—never automatically rewriting weights or prompts.
- **Human-in-the-Loop Safeguard**: First-touch cold emails require approval before entering dispatch queues.

---

## Testing

The codebase includes an exhaustive test suite covering all pipeline stages, edge cases, domain normalization, pattern deduction, scoring algorithms, quality gates, cloud automation, and mode-specific allocations.

```bash
# Run the entire test suite
python -m pytest -v

# Run Phase 3 mode and allocation tests specifically
python -m pytest tests/test_phase3_modes_and_allocation.py -v
```

All **198 tests pass** cleanly.
