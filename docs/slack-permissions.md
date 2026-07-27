# Slack Permissions

The bot is installed into one Slack workspace. Slack identifies the user, then the API checks what that user is allowed to do.

## OAuth Scopes

```text
users:read
users:read.email
chat:write
im:write
im:history
commands
files:read
files:write
```

## Why These Are Needed

`users:read` and `users:read.email`
: Sync Slack users into the employee table and map them by email.

`chat:write`
: Send bot responses, approval cards, request status updates, and balance messages.

`im:write`
: Let the bot open direct messages with employees, managers, and HR.

`im:history`
: Receive employee DM events so FastEmbed can route normal messages.

`commands`
: Register the leave slash commands.

`files:read`
: Download employee documents from Slack before storing them in managed storage.

`files:write`
: Upload complete employee leave balance CSV reports to the requesting manager or HR user.

## Optional Later

`im:read`, `app_mentions:read`, `channels:history`
: Only needed for extra DM metadata or channel use. `app_mentions:read` is
required if employees will mention the bot in channels, and `channels:history`
is required if the bot reads channel messages.

## Backend Checks Still Required

Slack authentication proves the event came from Slack. It does not prove the user can approve a request or view a balance. The API must still check the employee table, manager relationship, and HR role.
