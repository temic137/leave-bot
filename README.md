# Slack Leave Bot Foundation

This is the foundation for a free-flow Slack leave bot.

Current scope:

- Slack workspace sync is the employee source.
- Manager relationships come from an admin CSV.
- Leave/document rules are placeholders in JSON config.
- Slack events are acknowledged after they are durably queued in PostgreSQL.
- Groq interprets messages, AgentSpan owns approval workflow checkpoints, and PostgreSQL owns business data.
- Failed Slack and AgentSpan operations are retried with idempotency protection.

## Architecture

```text
Slack -> API -> PostgreSQL durable_jobs -> worker -> Groq parser
                                                -> business tables
                                                -> AgentSpan
                                                -> Slack replies/cards
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
```

Production requests to `/admin/*` and `/prototype/*` must include the
`X-Admin-API-Key` header. Slack events remain available at `/slack/events`.

## First MVP Flow

1. Seed or sync employees from Slack.
2. Upload manager mapping CSV.
3. Employee sends a free-flow leave message.
4. LLM parser extracts request fields.
5. API validates policy, balance, permissions, and document requirement.
6. A durable job starts the approval workflow and notifies the manager.
7. Manager/HR decisions are processed idempotently through durable jobs.
8. Approved requests are summed to report days taken.

## Slack Scopes To Request

```text
app_mentions:read
channels:history
chat:write
commands
files:read
im:history
im:read
im:write
users:read
users:read.email
```

These are captured in `docs/slack-permissions.md`.
