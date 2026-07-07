# Remaining Decisions Before Full Build

This file tracks what still needs to be determined before the foundation becomes a real production-ready leave bot.

## 1. Slack App Setup

- Confirm whether employees will use DM only, `/leave` slash command only, or both.
- Confirm the final Slack OAuth scopes.
- Decide whether the bot can read channel messages or only direct messages.
- Decide where Slack approval cards should be sent: manager DM, HR DM, or approval channel.
- Decide whether Slack file uploads are allowed directly in the bot conversation.

Current assumption:

```text
DM-first free-flow messages, optional /leave command, manager/HR approvals by DM.
```

## 2. Employee Sync

- Confirm how often Slack users should sync into the database.
- Decide whether inactive/deactivated Slack users should be disabled automatically.
- Decide whether contractors, bots, guests, and external users should be excluded.
- Decide who can trigger a manual sync.
- Decide whether employee profile images/timezones should be stored.

Current assumption:

```text
Slack is the employee source, and email is the matching key.
```

## 3. Manager Mapping

- Confirm the CSV format for manager mapping.
- Decide who owns and uploads the manager mapping CSV.
- Decide what should happen if a user has no manager.
- Decide what should happen if the manager email is not found in Slack.
- Decide whether managers can have backup approvers.

Current assumption:

```text
Manager relationships come from admin CSV:
employee_email,manager_email,role,department
```

## 4. Leave Policy

- Define final leave types.
- Define annual allowance for each leave type.
- Decide whether weekends count as leave days.
- Decide whether public holidays count as leave days.
- Decide whether half-days are allowed.
- Decide whether negative balances are allowed.
- Decide whether unused leave carries over to the next year.
- Decide whether new employees receive prorated leave.
- Decide whether balances reset on calendar year or employee anniversary.

Current assumption:

```text
Placeholder rules live in config/leave_policy.json.
```

## 5. Document Rules

- Decide which leave types require documents.
- Decide whether documents are required before submission or can be added later.
- Decide accepted file types: PDF, PNG, JPG, JPEG, etc.
- Decide max file size.
- Decide who can view documents.
- Decide whether managers can view sensitive medical documents or only HR can.
- Decide how long signed document URLs should remain valid.

Current assumption:

```text
Documents are stored outside the DB and referenced by document_key.
```

## 6. Approval Rules

- Define which leave types require manager only.
- Define which leave types require manager plus HR.
- Decide whether HR approval happens after manager approval or in parallel.
- Decide whether rejection by manager stops the process immediately.
- Decide whether employees can cancel pending requests.
- Decide whether managers can request changes instead of approve/reject.
- Decide what happens if a manager does not respond after a deadline.
- Decide whether reminders and escalations are needed.

Current assumption:

```text
Manager approval first. If policy requires HR, HR approval comes second.
```

## 7. Balance Rules

- Decide how initial balances are created.
- Decide who can adjust balances manually.
- Decide whether balance changes require approval.
- Decide how to handle corrections and reversals.
- Decide whether rejected/cancelled requests should affect balance.
- Decide whether approved requests deduct balance immediately or on leave start date.
- Decide whether admins still need an internal remaining-entitlement view later.

Current assumption:

```text
Approved leave writes a negative entry into leave_balance_ledger.
The employee view shows accumulated taken days only.
```

## 8. LLM Behavior

- Choose the LLM provider.
- Define the final structured output schema.
- Decide confidence threshold for accepting parsed messages.
- Decide when the bot should ask clarification questions.
- Decide how to handle ambiguous dates like "next Friday".
- Decide whether the LLM can classify leave type or only extract explicit values.
- Decide what safety checks should run after parsing.

Current assumption:

```text
LLM parses free-flow text only. API enforces all business rules.
```

## 9. Agentspan Workflow

- Confirm the exact Agentspan API methods to use.
- Decide what data is stored in Agentspan versus the database.
- Decide how Slack button clicks resume waiting workflows.
- Decide how long workflows can remain waiting.
- Decide how to recover if the API receives duplicate Slack interactions.
- Decide how workflow failures are reported to admins.

Current assumption:

```text
Agentspan owns durable waiting. The API owns DB updates, permissions, and notifications.
```

## 10. Storage

- Choose local storage for development and S3 for production.
- Decide S3 bucket name and region.
- Decide folder/key structure for uploaded documents.
- Decide encryption and retention rules.
- Decide whether documents should be deleted after a retention period.

Current assumption:

```text
Store files in S3 later. Store only document_key in the database.
```

## 11. Security

- Verify Slack request signatures on all Slack webhook routes.
- Add authorization checks for every balance, request, document, and approval action.
- Decide admin roles.
- Decide how production secrets are stored.
- Decide logging rules so sensitive documents/reasons are not leaked.
- Decide audit retention requirements.

Current assumption:

```text
Slack proves identity. The API enforces permissions.
```

## 12. Deployment

- Choose hosting provider for the API.
- Choose production database provider.
- Choose whether to use managed Postgres.
- Choose whether local development uses ngrok or Cloudflare Tunnel.
- Decide CI/test pipeline.
- Decide backup and restore process.

Current assumption:

```text
FastAPI backend, Postgres in production, SQLite/local mocks for foundation work.
```

## 13. Admin Operations

- Decide whether admin tools are CLI scripts or a web dashboard.
- Add command to sync Slack users.
- Add command to import manager mapping CSV.
- Add command to initialize annual balances.
- Add command to adjust balances.
- Add command to inspect pending requests.
- Add command to retry failed notifications.

Current assumption:

```text
Start with admin endpoints/scripts. Build dashboard later only if needed.
```
