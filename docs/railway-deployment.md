# Railway Deployment Checklist

Use this while waiting for the Slack app approval so the API is ready to receive real Slack events as soon as approval lands.

## What We Need Ready

- Railway project
- Railway API service deployed from this repo
- Railway PostgreSQL database
- Production environment variables
- Public Railway domain
- Slack Event Subscription URL pointed to Railway
- You and your manager registered in the app database

## 1. Push This Repo To GitHub

Railway can deploy directly from a GitHub repository.

The API container runs `alembic upgrade head` before starting Uvicorn. This applies schema changes to both a new database and the existing prototype database.

Make sure these files are committed:

```text
railway.json
.python-version
pyproject.toml
app/
config/
prototype/
```

Do not commit `.env`.

## 2. Create Railway Project

In Railway:

```text
New Project -> Deploy from GitHub repo
```

Select this repo.

Railway will use `railway.json`:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## 3. Add PostgreSQL

In the Railway project:

```text
+ New -> Database -> PostgreSQL
```

Railway provides a `DATABASE_URL` variable from the Postgres service.

## 4. Set API Service Variables

Open the API service:

```text
Variables -> RAW Editor
```

Add:

```text
APP_NAME=Slack Leave Bot
DATABASE_URL=${{Postgres.DATABASE_URL}}
LEAVE_POLICY_PATH=config/leave_policy.json
MANAGER_MAPPING_CSV=config/manager_mapping.sample.csv
SLACK_BOT_TOKEN=xoxb-your-approved-token
SLACK_SIGNING_SECRET=your-signing-secret
```

Leave these blank for now unless needed:

```text
OPENAI_API_KEY=
AGENTSPAN_API_KEY=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-1
S3_BUCKET_NAME=
```

## 5. Generate Railway Public Domain

Open the API service:

```text
Settings -> Networking -> Generate Domain
```

You will get a URL like:

```text
https://your-service.up.railway.app
```

Check liveness:

```text
https://your-service.up.railway.app/health
```

Expected:

```json
{"status":"ok"}
```

Check readiness:

```text
https://your-service.up.railway.app/health/ready
```

Check the durable queue and dependency configuration:

```text
https://your-service.up.railway.app/health/dependencies
```

## 6. Configure Slack Event URL

After Slack app approval, go to Slack app dashboard:

```text
Event Subscriptions -> Request URL
```

Use:

```text
https://your-service.up.railway.app/slack/events
```

Subscribe to bot event:

```text
message.im
```

Save changes.

## 7. Register You And Manager

Open Railway public URL:

```text
https://your-service.up.railway.app
```

Use **Slack Directory** if token/scopes allow it.

If Slack Directory is blocked, add manually in **Admin People**:

Manager:

```text
Name: Manager name
Email: manager@manual.local
Slack user ID: manager Slack ID
Role: manager
Manager: None
```

You:

```text
Name: Your name
Email: you@manual.local
Slack user ID: your Slack ID
Role: employee
Manager: select your manager
```

## 8. Test In Slack

DM the bot:

```text
show my leave balance
```

Then:

```text
i want annual leave from 8th of July to 9th of July
```

Manager should receive a bot DM:

```text
approve request 1
```

Then you ask:

```text
show my leave balance
```

Expected:

```text
annual: 2 days taken
```

## Current Deployment Status

Implemented:

- Alembic database migrations.
- Production admin API-key protection.
- Slack approval buttons and natural-text approvals.
- Durable PostgreSQL job queue with retries and idempotency.
- Durable AgentSpan manager and HR workflow stages.
- Structured logs and liveness, readiness, and dependency endpoints.

Still incomplete:

- Slack file upload/document storage, intentionally deferred.
- Production alert delivery for dead jobs.
- A protected admin action for manually replaying dead jobs.
- Live failure testing against the deployed Slack, Groq, AgentSpan, and Supabase services.

For pilot testing, this is enough to prove:

- Railway API is reachable by Slack.
- Slack signature verification works.
- Employee can request leave in Slack.
- Manager can approve/reject in Slack.
- Leave taken updates after approval.
