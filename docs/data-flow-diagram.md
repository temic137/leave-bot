# Leave Bot System Architecture And Data Flow

This is the canonical description of the currently deployed leave bot. It covers the components, request flows, databases, tables, fields, relationships, permissions, and external services.

All diagrams in this document are plain text. They are not Mermaid diagrams.

## 1. System Purpose

Employees interact with the leave bot through natural Slack messages. The bot can:

- Report how many approved leave days an employee has taken.
- Collect a leave request over one or more natural messages.
- Read the active leave policy and determine whether a document or HR approval is required.
- Send an approval card to the employee's manager.
- Record manager and HR decisions.
- Preserve accepted Slack events and approval workflows when the API restarts.
- Maintain an auditable business record in PostgreSQL.

## 2. Deployed Components

```text
+------------------+
| Employee/Manager |
+--------+---------+
         |
         | Slack messages and approval button clicks
         v
+------------------+       signed HTTPS webhooks       +------------------+
| Slack Workspace  | ---------------------------------> | FastAPI Leave Bot|
| and Slack App    | <--------------------------------- | Railway service  |
+------------------+          bot responses             +---+---+---+------+
                                                             |   |   |
                              +------------------------------+   |   +----------------+
                              |                                  |                    |
                              v                                  v                    v
                    +------------------+               +------------------+  +------------------+
                    | Groq LLM         |               | Leave PostgreSQL |  | AgentSpan Server |
                    | message parsing  |               | Supabase         |  | Railway          |
                    +------------------+               +------------------+  +--------+---------+
                                                                                     |
                                                                                     v
                                                                            +------------------+
                                                                            | AgentSpan        |
                                                                            | PostgreSQL       |
                                                                            | Railway          |
                                                                            +------------------+

Future document path:

FastAPI Leave Bot ------------> S3-compatible bucket
                                 (configured, not yet wired end to end)
```

### 2.1 Slack

Slack is the user interface and identity source.

Responsibilities:

- Delivers employee messages to `POST /slack/events`.
- Delivers approval button actions to `POST /slack/interactions`.
- Identifies users with stable Slack user IDs.
- Displays bot replies and manager approval cards.
- Provides workspace directory users through `users.list`.

Slack is not the business database. It does not own manager relationships, policies, leave requests, or balances.

### 2.2 FastAPI API

The API is the business-logic boundary.

Responsibilities:

- Verifies Slack request signatures and timestamps.
- Stores each Slack event with a unique idempotency key and acknowledges Slack immediately.
- Runs a PostgreSQL-backed worker for inbound events and outbound side effects.
- Loads employees and manager relationships.
- Calls Groq to interpret natural messages.
- Applies deterministic policy and permission checks.
- Creates and updates leave requests.
- Starts and advances AgentSpan approval executions.
- Writes approval audit events.
- Calculates days taken from approved requests.
- Sends Slack messages.
- Serves the protected administration UI.

The API must authorize every operation. Slack membership alone does not grant manager, HR, or administrator permissions.

### 2.3 Groq LLM

Groq is used only to understand natural employee messages.

Current model configuration:

```text
Provider: Groq
Default model: qwen/qwen3.6-27b
```

The parser receives:

- The current date.
- Valid leave types from the active policy.
- Fields already collected in the current conversation.
- The employee's latest message.

It returns structured fields such as intent, leave type, dates, reason, confidence, and missing fields.

Groq does not approve leave, enforce permissions, write the database, or calculate days taken. If Groq fails, the API has a deterministic parser fallback.

### 2.4 AgentSpan

AgentSpan is the durable approval workflow engine.

Responsibilities:

- Creates a durable execution for each submitted leave request.
- Holds manager and HR human-approval tasks.
- Persists workflow state outside the API process.
- Allows a workflow to survive API restarts and deployments.
- Records execution status and task history.
- Terminates the execution when a request is rejected.

The approval workflows are deterministic human-task workflows. They do not use an LLM to decide whether leave should be approved.

Two workflow definitions exist:

```text
leave_approval_manager_v1
  manager_approval -> completed

leave_approval_manager_hr_v1
  manager_approval -> hr_approval -> completed
```

AgentSpan does not own employees, policies, final leave records, days-taken totals, or the company approval audit. Those remain in the leave database.

AgentSpan has its own Railway PostgreSQL database. Its internal tables are infrastructure details and are not part of the leave bot's five business tables.

### 2.5 Leave PostgreSQL Database

The leave database is the source of truth for business data.

It stores:

- Employees, roles, and manager relationships.
- Leave requests and final statuses.
- Approval audit events.
- Policy versions.
- Temporary fields collected across Slack messages.

It contains five business tables and one infrastructure table named `durable_jobs`.

`durable_jobs` preserves accepted Slack events, AgentSpan operations, and outbound Slack messages. Jobs are retried with exponential backoff, stale processing locks are recovered after restarts, and exhausted jobs remain available for diagnosis.

### 2.6 S3-Compatible Bucket

The configuration contains AWS/S3 settings for leave documents.

Intended responsibilities:

- Store medical certificates and other required documents.
- Return an object key, not a public URL, to the API.
- Keep document binaries out of PostgreSQL.
- Allow short-lived signed access for authorized reviewers.

Current status: the `document_key` field and S3 configuration exist, but Slack file download, S3 upload, malware scanning, signed retrieval, and retention handling are not yet implemented end to end.

### 2.7 Railway

Railway hosts:

- The FastAPI leave bot service.
- The AgentSpan Java service.
- AgentSpan's PostgreSQL database.

The API reaches AgentSpan through Railway private networking:

```text
http://agentspan.railway.internal:6767
```

The API and AgentSpan use separate Dockerfiles and separate start commands.

## 3. Authentication And Authorization

### 3.1 Slack Request Authentication

Slack signs every event and interaction request. The API:

1. Reads the raw request body.
2. Reads `X-Slack-Signature` and `X-Slack-Request-Timestamp`.
3. Rejects timestamps older than five minutes.
4. Recomputes the HMAC-SHA256 signature using `SLACK_SIGNING_SECRET`.
5. Uses a constant-time comparison.
6. Rejects invalid requests before reading or changing business data.

### 3.2 Slack API Authentication

The API uses `SLACK_BOT_TOKEN` to call Slack APIs and send messages. The token identifies the Slack app, not the employee.

### 3.3 Employee Identity

The Slack event supplies a Slack user ID. The API finds the employee with:

```text
employees.slack_user_id = Slack event user ID
```

An unknown Slack user cannot query business data or submit leave until an employee record is created.

### 3.4 Role Permissions

```text
Employee
  - Submit their own leave request
  - View their own days taken
  - View their own request result

Manager
  - All employee actions
  - View days taken for direct reports
  - Approve or reject direct-report requests

HR
  - View employee leave information
  - Approve requests currently at the HR stage

Admin
  - Manage employees and manager assignments
  - Edit leave policy text
  - View policy history and administration state
```

Manager access is enforced using `employees.manager_id`. A manager cannot approve an employee who does not report to them.

The production administration API is protected by `X-Admin-API-Key`.

## 4. Employee And Manager Setup Flow

```text
Admin
  |
  | opens Slack Directory in the admin UI
  v
FastAPI calls Slack users.list
  |
  | returns names, emails, and Slack user IDs
  v
Admin creates or updates manager employee record
  |
  | role = manager
  v
Admin creates or updates employee record
  |
  | manager_id = manager's employees.id
  v
employees table now contains the reporting relationship
```

The manager may be created before or after the employee. If the employee already exists, the admin updates that same employee record rather than creating a duplicate Slack user ID.

## 5. Natural Conversation Flow

Example:

```text
Employee: I need sick leave.
Bot: What dates do you need?
Employee: It starts on the 8th of July and ends on the 10th.
```

Data flow:

```text
Slack message
  |
  v
POST /slack/events
  |
  +--> verify Slack signature
  +--> insert process_slack_event job using Slack event_id
  +--> return HTTP 200 to Slack
  |
  v
durable worker
  |
  +--> load employee by slack_user_id
  |
  +--> load latest policy version
  |
  +--> load open conversation_sessions fields
  |
  +--> Groq parser combines old fields with new message
  |
  +--> missing fields?
         |
         +--> yes: save collected fields and ask naturally for what is missing
         |
         +--> no: validate policy, dates, document requirement, and manager
                    |
                    v
                  create leave_requests row
                    |
                    v
                  queue start_agentspan job
                    |
                    v
                  retry AgentSpan until execution starts
                    |
                    v
                  store agentspan_execution_id
                    |
                    v
                  queue and retry manager Slack approval card
```

`conversation_sessions` is needed because separate Slack messages are separate HTTP requests. AgentSpan handles the submitted approval workflow; it does not reliably correlate partially collected fields from unrelated Slack webhook requests.

## 6. Manager Approval Flow

```text
Manager clicks Approve in Slack
  |
  v
POST /slack/interactions
  |
  +--> verify Slack signature
  +--> insert process_slack_interaction job
  +--> return immediately
  |
  v
durable worker
  |
  +--> load approver employee
  +--> load leave request
  +--> verify approver is employee's manager
  +--> ask AgentSpan to complete active manager_approval task
  |
  +--> policy requires HR?
         |
         +--> no
         |    - leave_requests.status = approved
         |    - leave_requests.decided_at set
         |    - approval_events manager approval inserted
         |    - employee notified
         |
         +--> yes
              - AgentSpan advances to hr_approval
              - leave_requests.status = pending_hr
              - approval_events manager approval inserted
              - active HR/admin users receive approval cards
```

If AgentSpan cannot record the decision, the job retries and the business database remains unchanged. After the maximum attempts, the job is marked `dead` for investigation instead of being discarded.

## 7. Rejection Flow

```text
Manager or HR clicks Reject
  |
  v
API validates permission and current request stage
  |
  v
AgentSpan execution is terminated with a rejection reason
  |
  v
approval_events rejection inserted
  |
  v
leave_requests.status = rejected
  |
  v
leave_requests.decided_at set
  |
  v
employee notified in Slack
```

## 8. Days-Taken Query Flow

The product currently reports days taken, not allocated or remaining entitlement.

```text
Employee: Show my leave balance.
  |
  v
API identifies employee from Slack user ID
  |
  v
For each leave type, PostgreSQL calculates:

SUM(leave_requests.days_requested)
WHERE employee_id = current employee
  AND status = 'approved'
  AND leave_type = requested leave type
  AND start_date is in requested year
  |
  v
Bot replies with approved days taken per leave type
```

There is no `leave_balance_ledger` table. Approved `leave_requests` are the authoritative source, so a second ledger would duplicate data and could become inconsistent.

If allocation, carry-over, earned leave, or manual adjustments are introduced later, add a dedicated entitlement/adjustment design at that time.

## 9. Policy Editing Flow

```text
Admin opens policy text editor
  |
  v
Admin edits plain-language policy text
  |
  v
PUT /admin/leave-policy-text
  |
  +--> parse each leave type rule
  +--> reject invalid policy text
  +--> calculate next version number
  +--> insert immutable leave_policy_versions row
  +--> make latest version active
```

Example policy text:

```text
Annual Leave: 20 days maximum. No document required. Manager approval only.
Sick Leave: 10 days maximum. Document required. Manager approval only.
Maternity Leave: 90 days maximum. Document required. HR approval required.
```

The maximum-day text is policy information. It is not currently used as an allocated balance because the product reports days taken only.

## 10. Database Tables And Fields

### 10.1 `employees`

Purpose: stores Slack identity, organizational role, and reporting relationship.

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `id` | integer | yes | Primary key. |
| `slack_user_id` | varchar(64) | yes | Unique Slack member ID used for identity matching. |
| `email` | varchar(255) | yes | Unique employee email. |
| `name` | varchar(255) | yes | Display name. |
| `role` | varchar(32) | yes | `employee`, `manager`, `hr`, or `admin`. |
| `department` | varchar(128) | no | Optional organizational department. |
| `manager_id` | integer | no | Foreign key to another `employees.id`. |
| `is_active` | boolean | yes | Whether the employee can use the system. |
| `created_at` | datetime | yes | Creation timestamp. |
| `updated_at` | datetime | yes | Last update timestamp. |

Important constraints:

- `slack_user_id` is unique.
- `email` is unique.
- `manager_id` creates the manager-to-direct-report relationship.

### 10.2 `leave_requests`

Purpose: authoritative record of every submitted leave request.

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `id` | integer | yes | Primary key and request number shown in Slack. |
| `employee_id` | integer | yes | Foreign key to the requesting employee. |
| `leave_type` | varchar(64) | yes | Policy key such as `annual` or `sick`. |
| `start_date` | date | yes | First leave date. |
| `end_date` | date | yes | Last leave date. |
| `days_requested` | numeric(6,2) | yes | Calculated leave duration. |
| `reason` | text | no | Employee's reason or collected message text. |
| `document_key` | varchar(512) | no | Future S3 object key for an attached document. |
| `status` | varchar(32) | yes | Current business status. |
| `agentspan_execution_id` | varchar(255) | no | Durable AgentSpan workflow execution ID. |
| `created_at` | datetime | yes | Submission timestamp. |
| `decided_at` | datetime | no | Final approval or rejection timestamp. |

Status values:

```text
draft
pending_manager
pending_hr
approved
rejected
cancelled
```

Only `approved` requests count toward days taken.

### 10.3 `approval_events`

Purpose: immutable company-facing audit record of approval decisions.

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `id` | integer | yes | Primary key. |
| `leave_request_id` | integer | yes | Foreign key to `leave_requests.id`. |
| `approver_id` | integer | yes | Foreign key to the approving employee. |
| `approver_role` | varchar(32) | yes | Approval stage, normally `manager` or `hr`. |
| `decision` | varchar(32) | yes | `pending`, `approved`, or `rejected`. |
| `comment` | text | no | Decision comment or source description. |
| `created_at` | datetime | yes | Decision timestamp. |

This table remains even though AgentSpan has execution history. AgentSpan history is operational; `approval_events` is the business audit record used for reporting and compliance.

### 10.4 `leave_policy_versions`

Purpose: stores every immutable version of the administrator's policy text.

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `id` | integer | yes | Primary key. |
| `version` | integer | yes | Unique increasing policy version. |
| `raw_text` | text | yes | Human-editable policy document. |
| `rules_json` | text | yes | Parsed structured snapshot used by the application. |
| `created_by` | varchar(255) | yes | Actor that saved the version; currently admin/system text. |
| `created_at` | datetime | yes | Version creation timestamp. |

The latest `version` is the active policy. Older rows provide history and rollback information.

### 10.5 `conversation_sessions`

Purpose: temporarily stores fields collected across multiple Slack messages.

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `id` | integer | yes | Primary key. |
| `slack_user_id` | varchar(64) | yes | Slack user whose conversation is being collected. |
| `current_intent` | varchar(64) | no | Current operation, normally `create_leave_request`. |
| `collected_fields_json` | text | yes | JSON containing partial leave type, dates, and reason. |
| `status` | varchar(32) | yes | `open` while collecting or `closed` after submission. |
| `updated_at` | datetime | yes | Last conversation update. |

This table does not store complete Slack chat history. It stores only the structured fields required to finish the current request.

### 10.6 `durable_jobs`

Purpose: infrastructure queue for restart-safe and retryable work.

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `id` | integer | yes | Primary key. |
| `job_type` | varchar(64) | yes | Handler such as `process_slack_event`, `start_agentspan`, or `send_slack_message`. |
| `idempotency_key` | varchar(255) | yes | Unique stable key that suppresses duplicate delivery. |
| `payload_json` | text | yes | Minimum data required to execute the job. |
| `status` | varchar(32) | yes | `pending`, `processing`, `succeeded`, or `dead`. |
| `attempts` | integer | yes | Number of times the job has been claimed. |
| `max_attempts` | integer | yes | Retry limit. |
| `available_at` | datetime | yes | Earliest time the worker may retry the job. |
| `locked_at` | datetime | no | Claim time used to recover work after a worker crash. |
| `last_error` | text | no | Most recent failure description. |
| `created_at` | datetime | yes | Creation timestamp. |
| `updated_at` | datetime | yes | Last state-change timestamp. |

This table has no business foreign keys. Payloads carry identifiers, while each handler reloads and authorizes current business records before acting.

## 11. Text ERD

```text
+-----------------------------+
| employees                   |
|-----------------------------|
| PK id                       |
| UQ slack_user_id            |
| UQ email                    |
| name                        |
| role                        |
| department                  |
| FK manager_id -> employees  |----+
| is_active                   |    |
| created_at                  |    |
| updated_at                  |    |
+-------------+---------------+    |
              ^                    |
              | employee_id        |
              |                    |
+-------------+---------------+    |
| leave_requests              |    |
|-----------------------------|    |
| PK id                       |    |
| FK employee_id              |    |
| leave_type                  |    |
| start_date                  |    |
| end_date                    |    |
| days_requested              |    |
| reason                      |    |
| document_key                |    |
| status                      |    |
| agentspan_execution_id      |    |
| created_at                  |    |
| decided_at                  |    |
+-------------+---------------+    |
              ^                    |
              | leave_request_id   |
              |                    |
+-------------+---------------+    |
| approval_events             |    |
|-----------------------------|    |
| PK id                       |    |
| FK leave_request_id         |    |
| FK approver_id -> employees |----+
| approver_role               |
| decision                    |
| comment                     |
| created_at                  |
+-----------------------------+

+-----------------------------+     +-----------------------------+
| leave_policy_versions       |     | conversation_sessions       |
|-----------------------------|     |-----------------------------|
| PK id                       |     | PK id                       |
| UQ version                  |     | slack_user_id               |
| raw_text                    |     | current_intent              |
| rules_json                  |     | collected_fields_json       |
| created_by                  |     | status                      |
| created_at                  |     | updated_at                  |
+-----------------------------+     +-----------------------------+

+-----------------------------+
| durable_jobs                |
|-----------------------------|
| PK id                       |
| job_type                    |
| UQ idempotency_key          |
| payload_json                |
| status                      |
| attempts / max_attempts     |
| available_at / locked_at    |
| last_error                  |
| created_at / updated_at     |
+-----------------------------+
```

Relationship summary:

```text
employees.manager_id       many employees -> one manager employee
leave_requests.employee_id many requests  -> one employee
approval_events.request_id many events    -> one leave request
approval_events.approver_id many events   -> one approver employee
```

`leave_policy_versions`, `conversation_sessions`, and `durable_jobs` have no database foreign keys to the other tables. Policy versions are global, conversation sessions are correlated through Slack user ID, and jobs reload referenced records before execution.

## 12. Data Ownership Rules

```text
Data                                      Owner
----------------------------------------  -------------------------
Slack user identity and message delivery Slack
Employee role and manager relationship   Leave PostgreSQL
Partial request fields                   Leave PostgreSQL
Policy and policy history                Leave PostgreSQL
Leave request and final status           Leave PostgreSQL
Business approval audit                  Leave PostgreSQL
Accepted events and retry state          Leave PostgreSQL durable_jobs
Durable approval execution state         AgentSpan PostgreSQL
Natural-language extraction              Groq, no business ownership
Document binary                          Future S3 bucket
Document object reference                leave_requests.document_key
```

## 13. Failure Behaviour

### Slack signature failure

The API returns an authentication error and performs no work.

### Unknown employee

The bot asks the user to have an administrator register their Slack user ID.

### Groq failure

The deterministic parser attempts to understand the message. If required fields remain missing, the bot asks a natural follow-up question.

### AgentSpan unavailable during submission

The leave request transaction is rolled back. The employee is told that no request was submitted.

### AgentSpan unavailable during approval

The business status and approval event are not changed. The approver is told to retry.

### API restart

Business data and accepted Slack events remain in PostgreSQL. Pending or stale jobs are reclaimed by the restarted worker, and submitted approval workflows remain in AgentSpan.

### Slack, Groq, or AgentSpan outage

Groq failures use the deterministic parser fallback. Slack delivery and AgentSpan operations remain in `durable_jobs` and retry with bounded exponential backoff. A permanent failure is marked `dead` instead of being discarded.

### Duplicate or old approval action

The API checks the request, current stage, and approver permissions before changing state. AgentSpan must also have exactly one active human task.

## 14. Environment Variables

```text
APP_NAME                  Application display name
APP_ENV                   development or production
ADMIN_API_KEY             Protects production admin endpoints
DATABASE_URL              Leave PostgreSQL connection string
LEAVE_POLICY_PATH         Development policy seed file

SLACK_BOT_TOKEN           Slack Web API bot token
SLACK_SIGNING_SECRET      Verifies Slack webhook signatures

GROQ_API_KEY              Groq authentication key
GROQ_MODEL                Natural-language parser model

AGENTSPAN_SERVER_URL      Private AgentSpan server URL

AWS_ACCESS_KEY_ID         Future S3 authentication
AWS_SECRET_ACCESS_KEY     Future S3 authentication
AWS_REGION                Future S3 region
S3_BUCKET_NAME            Future document bucket

JOB_WORKER_ENABLED         Run the durable worker in the API process
JOB_POLL_INTERVAL_SECONDS  Delay between empty queue polls
JOB_LOCK_TIMEOUT_SECONDS   Time before a crashed worker's job is reclaimed
JOB_MAX_ATTEMPTS           Maximum attempts before a job is marked dead
DB_POOL_SIZE               PostgreSQL connection pool size
DB_MAX_OVERFLOW            Additional temporary PostgreSQL connections
```

Secrets must be stored in Railway variables or another secret manager and must not be committed to Git.

## 15. Current Boundaries And Remaining Work

Implemented and deployed:

- Slack natural-message flow.
- Groq structured parsing with fallback.
- Employees and manager assignment.
- Plain-text policy editing and version history.
- PostgreSQL business persistence.
- AgentSpan PostgreSQL persistence.
- Manager approval buttons.
- Durable manager and HR workflow checkpoints.
- Approval audit events.
- Days-taken calculations from approved requests.

Not yet complete:

- Selecting and notifying a designated HR approver in Slack.
- Slack file ingestion and S3 document storage.
- Document access, scanning, and retention controls.
- Cancellation and approved-request reversal workflows.
- Production monitoring and alerts for failed AgentSpan executions.

## 16. Why There Are Five Business Tables And One Job Table

Each table has a separate business responsibility:

```text
employees              identity, roles, and reporting lines
leave_requests         authoritative leave records and days taken
approval_events        company approval audit
leave_policy_versions  editable policy history
conversation_sessions  partial fields across Slack webhook requests
durable_jobs           infrastructure queue, retries, and idempotency
```

`durable_jobs` is not business data and is excluded from leave reports. AgentSpan's internal tables are also not counted because they belong to a separate workflow service and separate database. No business report should query AgentSpan's internal database directly.
