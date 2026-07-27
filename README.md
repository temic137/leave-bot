# Slack Leave Bot Foundation

This is the foundation for a structured Slack leave bot.

Current scope:

- Slack workspace sync is the employee source.
- Manager relationships come from an admin CSV.
- Leave/document rules are placeholders in JSON config.
- Slack events are acknowledged after they are durably queued in PostgreSQL.
- FastEmbed routes normal employee messages to fixed actions without generating text.
- Slack commands and modals collect request data, AgentSpan owns approval workflow checkpoints, and PostgreSQL owns business data.
- Failed Slack and AgentSpan operations are retried with idempotency protection.
- Supporting documents are uploaded before manager approval begins.

## Architecture

```text
Slack DM -> FastEmbed intent -> explained Slack button/menu
                                      |
Slack commands/modals -> API -> PostgreSQL durable_jobs -> worker -> AgentSpan
                                  |                          |
                                  +-> business tables        +-> Slack replies/cards
```

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/docs
```

## Railway Environment

Configure these variables in Railway. Never commit their values:

```text
APP_ENV=production
ADMIN_API_KEY=<random-secret>
DATABASE_URL=<Supabase transaction pooler URL>
SLACK_BOT_TOKEN=<Slack bot token>
SLACK_SIGNING_SECRET=<Slack signing secret>
AUTOCHEK_UPLOAD_URL=https://api.staging.myautochek.com/document/upload
AUTOCHEK_API_TOKEN=<Autochek bearer token>
AUTOCHEK_API_KEY=<Autochek API key>
```

Production requests to `/admin/*` and `/prototype/*` must include the
`X-Admin-API-Key` header. Slack commands use `/slack/commands`, interactions
use `/slack/interactions`, and events remain available at `/slack/events`.

## First MVP Flow

1. Seed or sync employees from Slack.
2. Upload manager mapping CSV.
3. Employee sends a normal message or runs `/leave-request`.
4. FastEmbed selects a fixed action; requesting leave produces an explained button.
5. The button or command opens the Slack modal.
6. API validates the structured fields, policy, permissions, and document requirement.
7. A durable job uploads any document, then starts AgentSpan and notifies the manager.
8. Manager/HR decisions are processed idempotently through durable jobs.
9. Approved requests are summed to report days taken.

## Slack Scopes To Request

```text
app_mentions:read
channels:history
chat:write
commands
files:read
files:write
im:history
im:read
im:write
users:read
users:read.email
```

These are captured in `docs/slack-permissions.md`.
