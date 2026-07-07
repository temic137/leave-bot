# Slack Permissions

The bot is installed into one Slack workspace. Slack identifies the user, then the API checks what that user is allowed to do.

## OAuth Scopes

```text
users:read
users:read.email
chat:write
im:write
im:read
im:history
app_mentions:read
commands
files:read
channels:history
```

## Why These Are Needed

`users:read` and `users:read.email`
: Sync Slack users into the employee table and map them by email.

`chat:write`
: Send bot responses, approval cards, request status updates, and balance messages.

`im:write`, `im:read`, `im:history`
: Let employees DM the bot naturally and let the bot DM managers/HR.

`commands`
: Optional `/leave` entry point.

`app_mentions:read`
: Optional channel mention support.

`files:read`
: Download employee documents from Slack before storing them in S3 or local storage.

`channels:history`
: Only needed if the bot must read channel messages where it is used. Prefer DM-first for less scope.

## Backend Checks Still Required

Slack authentication proves the event came from Slack. It does not prove the user can approve a request or view a balance. The API must still check the employee table, manager relationship, and HR role.

