# Architecture Documentation: Agent Outreach Pipeline

## 1. Executive Summary & Design Principles

Agent Outreach is a production-grade, autonomous outreach pipeline built to operate either locally on macOS or unattended in the cloud via GitHub Actions.

### Core Tenets
1. **Unattended Safety**: The system never requires a local Mac to be awake or online. All scheduling, monitoring, enrichment, drafting, and sending can run in GitHub-hosted cloud runners.
2. **Quality Over Volume**: Max ceiling of **28 new emails per day**. Volume is never a target; if fewer than 28 prospects meet the required quality bar, the system sends fewer. Thresholds are never lowered just to fill quota.
3. **Unconstrained Follow-ups**: The 28/day limit strictly caps **new initial touches** (`step == 0`). Follow-ups are unconstrained by the 28 ceiling and proceed as scheduled to maintain pipeline velocity.
4. **Idempotency & Zero Duplicate Contact**: Strict deterministic keying (`idempotency_key = lead:{lead_id}:step:{step}`), atomic state claiming, and concurrency guards guarantee that no recipient receives duplicate outreach even in the event of job retries or runner crashes.
5. **Fail-Safe Operation**: In production (`OUTREACH_ENV=production`), the system will not dispatch an email unless outbound sending is explicitly enabled via `OUTREACH_SENDING_ENABLED=true`.

---

## 2. High-Level Architecture Diagram

```
                 +-------------------------------------------------+
                 |             Scheduled Trigger Sources           |
                 |  - GitHub Actions Cron (.github/workflows/)     |
                 |  - Local CLI / LaunchAgent (launchctl)          |
                 +-----------------------+-------------------------+
                                         |
                                         v
                 +-------------------------------------------------+
                 |            Central Send Engine (sender)         |
                 |  - Fail-safe Kill Switch & Dry-Run Engine       |
                 |  - Bounce Rate Sentinel (<= 5% across >= 20)    |
                 |  - Reconciler for Stale 'sending' States        |
                 +-----------------------+-------------------------+
                                         |
                                         v
                 +-------------------------------------------------+
                 |          Authoritative Eligibility Gate         |
                 |              (outreach/eligibility.py)          |
                 |  Gate A: Opportunity Quality (>= 75 score)      |
                 |  Gate B: Verified Contact (>= 80% confidence)   |
                 |  Gate C: Contact Relevance (Decision maker)     |
                 |  Gate D: Evidence Personalization (>= 1 fact)   |
                 |  Gate E: Personalization Confidence (>= 0.75)   |
                 |  Gate F: Explicit Human Approval                |
                 |  Gate G: Suppression & Previous Unsubscribe     |
                 |  Gate H: Contact History & Deterministic Key    |
                 |  Gate I: Quota: 28 New Cap (Followups Unlimited)|
                 |  Gate J: Sending Time Window (Local Timezone)   |
                 +-----------------------+-------------------------+
                                         |
                     +-------------------+-------------------+
                     |                                       |
                 [Eligible]                             [Blocked]
                     |                                       |
                     v                                       v
    +---------------------------------+             +-------------------+
    |    Safe Dispatch & Retries      |             | Held / Cancelled  |
    |  - Atomic DB Claim ('sending')  |             | Reason Recorded   |
    |  - Exponential Backoff Retry    |             +-------------------+
    |  - Zoho API / SMTP Transport    |
    |  - Outcome Tracking & Analytics |
    +---------------------------------+
```

---

## 3. Persistent State & Storage Architecture

### Local Development vs Remote Production
- **Local Development**: SQLite database at `data/outreach.db` with WAL (Write-Ahead Logging) and `PRAGMA busy_timeout = 30000`.
- **Remote Production (GitHub Actions / Cloud)**: Serverless SQLite via **Turso** (`libsql`) using HTTPS transport.
- When `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` are provided in the environment, `db.connect()` automatically connects to the remote Turso database.

### Schema & Core Tables
- `leads`: Master prospect entities, opportunity scoring components, email verification status, verified evidence facts, and deal pipeline status.
- `messages`: Outreach sequence steps (`step=0` initial, `step>0` follow-ups), draft body, subject, confidence score, approval state, sending timestamp, and `idempotency_key`.
- `send_log`: Daily send tallies partitioned by day, inbox, and type (`first`, `followup`, `first_freelance`, `first_internship`).
- `replies`: Inbound replies synchronized from mailboxes, categorized by LLM (`interested`, `not_interested`, `bounce`, etc.).
- `suppression`: Global exclusion list of emails, domains, and unsubscriptions.
- `message_outcomes`: Longitudinal conversion analytics recording mode, score, CTA, personalization facts, and final outcomes.

---

## 4. Opportunity Funnels: Freelance vs Internship

The system segregates lead evaluation into two dedicated funnels:

| Funnel | Target Contacts | Key Scoring Drivers | Personalization Hook | Low-Friction CTA |
| :--- | :--- | :--- | :--- | :--- |
| **FREELANCE** | Founder, CTO, Head of Ops, Technical Lead | Explicit project need, budget/commercial intent, technical pain, project urgency | Concrete evidence of understanding problem & technical proposal | *"I can outline how I'd implement this if useful."* |
| **INTERNSHIP** | Engineering Manager, Tech Recruiter, Founder | Active engineering hiring, skill alignment, team growth stage | Demonstrable project fit, github work, concrete contribution | *"Happy to send over a 2-minute demo if helpful."* |

---

## 5. Idempotency & Concurrency Model

### Deterministic Message Keying
Every message record enforces a unique index on `idempotency_key`:
$$\text{idempotency\_key} = \text{"lead:"} + \text{lead\_id} + \text{":step:"} + \text{step}$$
Any attempt to insert or queue a duplicate step for the same lead fails at the database constraint level.

### Safe Send Transitions
1. **Atomic Claim**: Before calling mail transport, the runner atomically sets `status = 'sending'`, `sending_at = now()`, and verifies `rowcount == 1`.
2. **Commit Isolation**: The database transaction is committed *prior* to contacting the email provider so network calls hold no database locks.
3. **Crash Recovery (`reconcile_stale_sending`)**: If a runner crashes or terminates during transmission, messages stuck in `'sending'` for > 15 minutes are moved to `'needs_reconciliation'` and an alert is dispatched via Telegram.

### GitHub Actions Concurrency Protection
Workflows utilize GitHub Actions concurrency locks:
```yaml
concurrency:
  group: outreach-sending
  cancel-in-progress: false
```
This guarantees that two workflow runs never dispatch outreach concurrently.

---

## 6. Fault Tolerance & Retries

### Error Classification (`outreach/retries.py`)
- **Transient Failures (Retried with Exponential Backoff)**:
  - HTTP 429 (Rate limit), HTTP 500, 502, 503, 504
  - Network timeouts, DNS drops, connection resets
  - SQLite database busy / locked
- **Permanent Failures (Immediate Abort, Never Retried)**:
  - SMTP 5xx permanent bounce (`SMTPRecipientsRefused`)
  - HTTP 400, 401, 403, 404, 422
  - Suppressed or unsubscribed recipient

---

## 7. Safety Sentinels & Alerting

### Bounce Protection Sentinel
- Automatically calculates 7-day rolling bounce rate per inbox and globally.
- Threshold: `MAX_BOUNCE_RATE` (default 5% / 0.05) with sample `MIN_BOUNCE_SAMPLE` (default 20).
- If exceeded: Inbox sending is immediately paused and an alert is dispatched.

### Central Alert Dispatcher (`outreach/alerts.py`)
- Integrated with Telegram Bot API.
- Implements anti-spam deduplication cooldowns (minimum 30 minutes between duplicate alerts).
- Dispatches alerts for: database outages, provider auth failures, bounce threshold breaches, duplicate send attempts, stuck sending states, and quota integrity warnings.
