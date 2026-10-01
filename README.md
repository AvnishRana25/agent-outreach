# agent-outreach

> An autonomous, zero-budget B2B cold outreach engine and pipeline desk. It finds high-intent companies from public data sources, deduces and verifies verified decision-maker emails (~38/day target), conducts deep AI research via Gemini with Groq fallback, drafts hyper-personalized multi-touch email sequences and LinkedIn touches, schedules safe deliveries across international business hours, triages replies, and manages deals in a unified web dashboard.

---

## Table of Contents

- [Overview & Architecture](#overview--architecture)
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
                                                 │ (Target: ~38 Verified Prospects/Day)
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

## Complete Feature Matrix

### 1. Multi-Source Lead Prospecting

The engine automatically collects high-intent leads across 8+ zero-cost public channels:

- **Remote Job Boards**: Scrapes Remotive, Himalayas, RemoteOK, Jobicy, and We Work Remotely for active hiring signals, technical requirements, and company websites.
- **Y Combinator Startup Directory**: Scrapes YC companies filtering by batch, hiring status, and target industry.
- **Hacker News Algolia API**: Real-time parsing of monthly "Who is hiring?", "Seeking freelancer?", and "Launch HN" threads.
- **UK Companies House**: Queries the official Companies House API for newly incorporated agencies, SIC code classifications, and officer/director rosters.
- **Dubai Land Department Register**: Scrapes registered UAE real estate brokerages, active license numbers, and managing directors.
- **OpenStreetMap (Overpass API)**: Extracts local businesses across specific geographic bounding boxes (e.g. Dubai, London, Bangalore).
- **Meta Ad Library Hunter**: Generates curated search links for advertisers running click-to-WhatsApp and lead ads in target regions.
- **Community Forum Monitor**: Monitors n8n forums, Reddit (`r/forhire`, `r/freelance_forhire`), flags `[Hiring]` posts every 30 minutes, drafts custom applications, and notifies via Telegram.
- **Custom CSV/JSON Importer**: Import your own lists with deduplication and segment assignment (`python -m outreach import leads.csv --segment uk_agencies`).

---

### 2. High-Confidence B2B Email Prospecting & Finder Engine

Engineered to discover verified professional email addresses for founders, CEOs, and decision-makers without paid APIs, targeting **~38 verified prospects per day**.

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
  - RFC 5322 syntax validation.
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
  - **Tiers**: `Verified (90-100%)`, `High Confidence (75-89%)`, `Needs Review (55-74%)`, `Unresolved (<55%)`.
- **Sequential Free-Tier Fallback Dispatcher**:
  - Only triggered when local confidence is insufficient (<75%).
  - Queries free tiers sequentially: **Prospeo** (100 free/mo) ➔ **Hunter** (50 free/mo) ➔ **Skrapp** (50 free/mo).
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
  - held until you press **Send anyway**: addresses not found on their website or a post (e.g. pattern guesses), research fit under 6;
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

- **Background Sync**: Checks inboxes every 20 minutes.
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
- **Weekly Thought Leadership**: Generates 3 LinkedIn post drafts every Monday based on your case studies and proof points.
- **Included Case Study Page**: Ready-to-deploy static case-study showcase (`site/index.html`).

---

### 9. Web Dashboard (Local & Vercel Cloud)

- **Dual Deployment Options**:
  - **Local**: `python -m outreach dashboard` serves locally on `http://127.0.0.1:7347`.
  - **Cloud**: Zero-maintenance serverless deployment on Vercel backed by a Turso edge database.
- **Security**: Password protected, secure HTTP-only session cookies, no API keys exposed to the client.
- **Key Desk Tabs**:
  - **Review**: Review drafted email sequences, inspect fit scores, edit copy, regenerate with custom instructions, approve, or reject.
  - **Prospecting Desk**: Dedicated UI for single prospect lookup, bulk CSV import, 38/day verified target meter, provider credit monitor, and full evidence audit modal.
  - **Respond / Inbox**: Unified inbox showing classified replies, AI drafted responses, and one-click sending.
  - **Deals**: Interactive CRM board for active negotiations with next-action dates.
  - **Engine**: Background daemon heartbeat, manual job runners, job logs, Ad Library search cards, and LinkedIn outreach tasks.
  - **Results**: Real-time conversion funnels, A/B angle comparison, and source channel attribution.

---

### 10. Hands-Free Background Daemon & Doctor

- **Native macOS `launchd` / Linux `cron`**: `python -m outreach install` configures a persistent daemon running every 5 minutes.
- **Autonomous Tick Workflow**:
  1. Synchronizes actions taken on the dashboard.
  2. Syncs inboxes and triages replies.
  3. Sends due emails within market business hours.
  4. Runs morning preparation (`prepare`) at 07:30 Mon-Sat.
  5. Pushes updated snapshots to Turso / Dashboard.
- **`doctor` Diagnostics**: Automatically inspects database integrity, background daemon status, pending sends, and connectivity.

---

## Pipeline Workflow

| Step | Command | Typical Frequency | Purpose |
|---|---|---|---|
| **Prospect** | `prospect` | Cron (in `prepare`) | Scrapes public directories, job boards, HN, UK/Dubai registers, OSM. |
| **B2B Finder** | `prospect-find` / `prospect-enrich` | On-demand / Dashboard | Discovers and validates emails; scores confidence 0-100; enforces 38/day target. |
| **Import** | `import <file.csv>` | On-demand | Bulk imports existing lists into a target segment. |
| **Enrich** | `enrich` | Daily | Scrapes website signals, team pages, and published company contacts. |
| **Verify** | `verify` | Daily | Validates syntax, DNS/MX records, and checks disposable/role addresses. |
| **Research** | `research` | Daily | Gemini research brief + Google News synthesis; computes 0-10 fit score. |
| **Draft** | `draft` | Daily | Drafts Email 1, two follow-ups, and LinkedIn note with A/B angles. |
| **Review** | `review` / `approve` | Daily (15-20 min) | Human review via terminal or dashboard (edit, approve, regenerate). |
| **Send** | `send` | Every 10 min | Safe scheduled delivery inside timezone windows with randomized gaps. |
| **Sync** | `sync` | Every 20 min | Fetches inbound replies, stops sequences, and triggers AI reply triage. |
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
python -m outreach prospect-enrich leads.csv --target 38 --workers 3 --output results.csv

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
python -m outreach draft --limit 38

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

# Fallback Email APIs (Optional free tiers)
PROSPEO_API_KEY=your_prospeo_key             # 100 free/mo
HUNTER_API_KEY=your_hunter_key               # 50 free/mo
SKRAPP_API_KEY=your_skrapp_key               # 50 free/mo

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
  min_fit: 6                  # Minimum research fit score (0-10)

sending:
  home_timezone: Asia/Kolkata
  # allowed_segments: [gulf_realestate, intl_freelance_posts]   # optional: only these segments send
  min_gap_minutes: 8
  gap_jitter_minutes: 7
  max_bounce_rate: 0.03       # Auto-pause threshold (3%)
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
    daily_new: 8
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
│   ├── db.py                     # SQLite database schema and ORM operations
│   ├── engine.py                 # Background daemon scheduler & tick loop
│   ├── enrich.py                 # Website crawler and published email extractor
│   ├── firecrawl.py              # Optional Firecrawl fallback client
│   ├── groq.py                   # Backup Groq LLM client
│   ├── growth.py                 # 1-page proposal and content generator
│   ├── importer.py               # CSV and list importer
│   ├── llm.py                    # Gemini client with retry & fallback logic
│   ├── personalize.py            # AI sequence generation
│   ├── prospect.py               # Directory and job board lead scrapers
│   ├── replies.py                # IMAP/Zoho reply fetcher & classifier
│   ├── report.py                 # Performance and conversion analytics
│   ├── research.py               # Company research and fit scoring
│   ├── review.py                 # Interactive terminal review interface
│   ├── sender.py                 # Safe scheduled send executor
│   ├── sources.py                # Public data source drivers
│   ├── transport.py              # SMTP, IMAP, and Zoho REST API transports
│   ├── turso.py                  # Turso edge database client
│   ├── verify.py                 # Email validation and lead scoring
│   ├── website.py                # Domain resolution and discovery
│   └── prospecting/              # Free B2B Email Prospecting Engine
│       ├── config.py             # Weights, thresholds, and provider credit caps
│       ├── models.py             # Pydantic data schemas
│       ├── domain/               # Normalization and domain resolution
│       ├── validation/           # RFC syntax, DNS/MX, role, catch-all
│       ├── email/                # Pattern deduction, permutations, scoring
│       ├── discovery/            # Site crawler, search, GitHub scanner
│       ├── providers/            # Prospeo, Hunter, Skrapp fallback clients
│       └── pipeline/             # Single and bulk multi-threaded processors
├── tests/                        # 107 comprehensive automated unit and integration tests
├── PLAYBOOK.md                   # Strategic market research & 30-day outreach playbook
├── GROWTH.md                     # High-ticket closing guide & conversion channels
└── requirements.txt              # Production Python dependencies
```

---

## Deliberate Design & Compliance Boundaries

- **Zero Guessed Emails**: The pipeline prioritizes publicly exposed addresses and confirmed patterns. Unverified emails are scored down and flagged for manual review.
- **Anti-Spam & Deliverability Standards**: Staggered sending gaps (8-15 mins), strict daily caps, automated bounce shutoffs, and immediate opt-out suppression keep sender domain reputation pristine.
- **Terms of Service Compliance**:
  - No automated LinkedIn scraping or robotic browser automation that risks account bans. LinkedIn tasks and texts are drafted for hand delivery.
  - No scraping of Facebook personal data or restricted directories.
- **Human-in-the-Loop Safeguard**: First-touch cold emails require approval before entering dispatch queues.

---

## Testing

The codebase includes an exhaustive test suite covering all pipeline stages, edge cases, domain normalization, pattern deduction, scoring algorithms, and API endpoints.

```bash
# Run the entire test suite
python -m pytest -q

# Run prospecting tests specifically
python -m pytest tests/test_prospecting.py -q
```

All **107 tests pass** cleanly.
