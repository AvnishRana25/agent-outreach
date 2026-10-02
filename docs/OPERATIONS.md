# Operations Guide: Agent Outreach Pipeline

## 1. Operating Modes Overview

The pipeline operates in two environments:
- **Local Development (`OUTREACH_ENV=local`)**: Runs against local SQLite (`data/outreach.db`). Used for local inspection, test suites, and manual CLI testing.
- **Production (`OUTREACH_ENV=production`)**: Runs unattended on GitHub Actions runners against Turso remote SQLite (`TURSO_DATABASE_URL`). Safe, authenticated, rate-limited, and idempotent.

---

## 2. Environment Variables & GitHub Secrets

### Required GitHub Secrets for Unattended Operation
Add the following secrets to **GitHub Repository Settings -> Secrets and variables -> Actions**:

| Secret Name | Description | Example / Format |
| :--- | :--- | :--- |
| `TURSO_DATABASE_URL` | Remote Turso database connection URL | `libsql://your-db-name.turso.io` |
| `TURSO_AUTH_TOKEN` | Turso authentication token | `eyJh...` |
| `GEMINI_API_KEY` | Primary LLM key for research & drafting | `AIzaSy...` |
| `GROQ_API_KEY` | Fallback LLM key for fast parsing & backup | `gsk_...` |
| `TELEGRAM_BOT_TOKEN` | Bot token for alerts & notifications | `123456789:ABCdef...` |
| `TELEGRAM_CHAT_ID` | Telegram user/group chat ID for alerts | `-100123456789` |
| `ZOHO_CLIENT_ID` | Zoho API OAuth client ID | `1000.XXXX` |
| `ZOHO_CLIENT_SECRET` | Zoho API OAuth client secret | `abcd...` |
| `ZOHO_REFRESH_TOKEN` | Zoho OAuth persistent refresh token | `1000.YYYY.ZZZZ` |
| `ZOHO_APP_PASSWORD` | Zoho SMTP fallback application password | `xxxx yyyy zzzz` |
| `SETTINGS_YAML` | Production settings override (optional) | Base64 or plain YAML |
| `PROFILE_YAML` | Sender profile and portfolio proof points | Base64 or plain YAML |

---

## 3. Pre-Flight Health Inspection: The Health Doctor

Before enabling outbound sending or investigating issues, run the non-destructive Health Doctor:

```bash
# Local development inspection
python -m outreach doctor

# Or in production runner
OUTREACH_ENV=production python -m outreach doctor
```

The doctor tests all 11 critical subsystems without sending an email:
1. Environment & YAML configuration check
2. Database connectivity (local & remote Turso ping)
3. Schema & idempotency index presence
4. Gemini & Groq model availability
5. Community & Reddit monitoring credentials
6. Inbox configuration & rolling 7-day bounce rates
7. Telegram alert bot ping
8. Kill-switch status (`OUTREACH_SENDING_ENABLED`)
9. Dry-run mode (`OUTREACH_DRY_RUN`)
10. Daily send counts, freelance/internship splits, and remaining 28 new email quota
11. In-flight stuck messages & reconciliation state

---

## 4. Quota Rules & Daily Allocation

- **Hard Ceiling on New Initial Outreach**: Max **28 new emails per day** (`step == 0`).
- **Follow-ups Are Unconstrained**: Sequences require timely follow-ups to maintain momentum. Follow-ups (`step > 0`) are not blocked by the 28 new email limit.
- **Configurable Split**: Default baseline is 16 Freelance / 12 Internship new emails.
- **Unused Quota Reallocation**: When enabled (`ALLOW_UNUSED_QUOTA_REALLOCATION=true`), unused slots from one category can be filled by high-quality opportunities from the other, up to the 28 ceiling.

---

## 5. Safe Dry-Run Testing

Verify candidate selection, opportunity scoring, and gate evaluations without sending messages:

```bash
# Via CLI
python -m outreach send --dry-run

# Or via environment variable
OUTREACH_DRY_RUN=true python -m outreach send
```

**Dry-run guarantees**:
- Discovers and scores candidates.
- Evaluates Gates A through J.
- Prints `[dry-run] <inbox> -> <recipient> step <step> [<mode>]: <subject>`.
- Consumes **0** send quota.
- Transmits **0** outbound emails.

---

## 6. Global Kill Switch & Incident Management

### Immediate Kill Switch
To immediately block all outbound email dispatch across all runners:

```bash
# In environment or GitHub Secrets / Workflow Inputs:
OUTREACH_SENDING_ENABLED=false
```

When set to `false`:
- Discovery, research, verification, drafting, analytics, and inbox reply sync **continue normally**.
- Outbound message transmission is **strictly blocked**.
- In production (`OUTREACH_ENV=production`), omitting `OUTREACH_SENDING_ENABLED` defaults safely to disabled.

### Automated Bounce Threshold Sentinel
- If an inbox's 7-day bounce rate exceeds 5% across >= 20 sends, it is paused automatically.
- If all inboxes exceed threshold, outreach stops and an alert is sent via Telegram.

---

## 7. Crash Recovery & Stale State Reconciliation

If a GitHub Actions runner is terminated mid-send:
- Messages in `'sending'` state for > 15 minutes are moved to `'needs_reconciliation'`.
- Telegram alert is dispatched: `⚠️ Stuck Sending State Detected`.
- **To resolve**:
  1. Inspect the Sent folder in your mail provider (Zoho/Gmail).
  2. If the email was sent, mark it as sent via CLI:
     ```bash
     python -c "import outreach.db as db; conn = db.connect(); conn.execute(\"UPDATE messages SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=<ID>\"); conn.commit()"
     ```
  3. If the email was not sent, reset to approved:
     ```bash
     python -c "import outreach.db as db; conn = db.connect(); conn.execute(\"UPDATE messages SET status='approved', error=NULL WHERE id=<ID>\"); conn.commit()"
     ```

---

## 8. Manual Workflow Dispatch in GitHub Actions

You can trigger any workflow manually from the GitHub web UI:
1. Navigate to **Actions** tab in your repository.
2. Select **Scheduled Outreach**.
3. Click **Run workflow**:
   - `sending_enabled`: set to `true` for live sending or `false` to block.
   - `dry_run`: check to execute safe dry-run.
   - `max_sends`: emails to send in this run (e.g. `2` or `5`).
   - `run_prospecting`: check if you want to run discovery & drafting first.
4. Click **Run workflow**.
