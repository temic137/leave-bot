# Slack Live Test Checklist

This is the smallest path to prove the bot works in real Slack with one employee and one manager.

## 1. Environment Variables

Create or update `.env`:

```text
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-signing-secret
DATABASE_URL=sqlite:///./leavebot.db
```

Restart the FastAPI server after editing `.env`.

## 2. Slack Bot Scopes

In Slack app dashboard, add these bot token scopes:

```text
chat:write
im:history
im:read
im:write
users:read
users:read.email
app_mentions:read
```

Reinstall the app to the workspace after changing scopes.

## 3. Public URL For Local Testing

Slack cannot call `http://127.0.0.1:8000`.

Use a tunnel:

```text
ngrok http 8000
```

Example tunnel URL:

```text
https://abc123.ngrok-free.app
```

## 4. Slack Event Subscription

In Slack app dashboard:

```text
Event Subscriptions -> Enable Events -> Request URL
```

Use:

```text
https://abc123.ngrok-free.app/slack/events
```

Slack should verify the URL challenge.

Subscribe to bot events:

```text
message.im
app_mention
```

For the simplest test, use DM with the bot, so `message.im` is the important event.

## 5. Start The Server

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 6. Sync Slack Users

Once `SLACK_BOT_TOKEN` is set, call:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/admin/sync/slack"
```

This imports real Slack users by email and Slack user ID.

## 7. Assign Manager

Open:

```text
http://127.0.0.1:8000
```

Use **Admin People** to:

- Confirm your employee record has role `employee`.
- Confirm your manager exists.
- Set your manager as your manager.
- Set your manager role to `manager`.

## 8. Test In Slack

Employee DM to bot:

```text
i want to check my leave balance
```

Employee leave request:

```text
i want annual leave from 8th of July to 9th of July
```

The bot should:

- Create the leave request.
- Reply to employee.
- DM the manager with approval instructions.

Manager DM to bot:

```text
approve request 1
```

or:

```text
reject request 1
```

Employee checks balance again:

```text
show my leave balance
```

After approval, annual leave taken should increase by the approved number of days.

## Current Slack Limitations

This live test uses text commands, not Slack buttons yet.

Not included yet:

- Slack interactive buttons
- File upload handling from Slack
- S3 document storage
- Agentspan durable waiting
- Production Postgres

This is enough to prove:

- Slack can reach the backend.
- Slack signatures are verified.
- Real Slack users map to employees.
- Employee can request leave by chat.
- Manager can approve/reject by chat.
- Leave taken updates after approval.

