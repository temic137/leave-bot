# Data Flow Diagram

This document describes the system using plain text data-flow diagrams.

## 1. High-Level System Flow

```text
Employee
   |
   | sends free-flow leave message / uploads document / checks balance
   v
Slack Workspace
   |
   | sends event, message, file event, or button interaction
   v
API Backend
   |
   | verifies Slack request signature
   | maps slack_user_id to employee
   | checks permissions
   | controls business rules
   |
   +---------------------> LLM Parser
   |                         |
   |                         | returns structured intent
   |                         v
   |<--------------------- parsed leave data
   |
   +---------------------> Database
   |                         |
   |                         | stores employees, requests, balances,
   |                         | approvals, conversations, sync runs
   |                         v
   |<--------------------- saved/read system state
   |
   +---------------------> S3 / Document Storage
   |                         |
   |                         | stores uploaded documents/images
   |                         v
   |<--------------------- document_key / signed URL
   |
   +---------------------> Agentspan
   |                         |
   |                         | waits for manager/HR approval
   |                         v
   |<--------------------- workflow status / resume result
   |
   v
Slack Workspace
   |
   | sends response, follow-up question, approval card, or result
   v
Employee / Manager / HR
```

## 2. Employee Sync Flow

```text
Admin triggers Slack sync
   |
   v
API Backend
   |
   | calls Slack users.list
   v
Slack Workspace
   |
   | returns Slack users:
   | - slack_user_id
   | - email
   | - name
   | - active/deactivated status
   v
API Backend
   |
   | filters users if needed:
   | - bots
   | - guests
   | - external users
   | - inactive users
   |
   | upserts employees by email
   v
Database
   |
   | stores/updates:
   | - employees.slack_user_id
   | - employees.email
   | - employees.name
   | - employees.is_active
   | - slack_sync_runs
   v
Employee database is ready for manager mapping
```

## 3. Manager Mapping Flow

```text
Admin prepares CSV
   |
   | CSV columns:
   | employee_email, manager_email, role, department
   v
API Backend
   |
   | reads CSV
   | validates required columns
   | stores mapping rows
   v
Database
   |
   | manager_mappings table updated
   v
API Backend
   |
   | for each mapping:
   | - find employee by employee_email
   | - find manager by manager_email
   | - set employee.manager_id
   | - set employee.role
   | - set employee.department
   v
Database
   |
   | employees table now has:
   | - manager relationship
   | - role
   | - department
   v
Approval routing can work
```

## 4. Free-Flow Leave Request Flow

```text
Employee sends message in Slack
Example: "I need sick leave from 2026-07-10 to 2026-07-12"
   |
   v
Slack Workspace
   |
   | sends event payload to API
   v
API Backend
   |
   | verifies Slack signature
   | extracts slack_user_id
   | finds employee in database
   v
Database
   |
   | returns employee record
   v
API Backend
   |
   | sends message text to LLM parser
   v
LLM Parser
   |
   | returns structured data:
   | - intent
   | - leave_type
   | - start_date
   | - end_date
   | - reason
   | - missing_fields
   | - confidence
   v
API Backend
   |
   | checks if required fields are missing
   |
   +-- if missing fields exist ------------------+
   |                                             |
   | saves partial conversation                  |
   v                                             |
Conversation Sessions table                     |
   |                                             |
   v                                             |
API sends follow-up question to Slack            |
   |                                             |
   v                                             |
Employee answers follow-up ----------------------+

API Backend
   |
   | if all required fields exist:
   | - loads leave policy
   | - validates leave type
   | - validates dates
   | - calculates days requested
   | - checks document requirement
   | - checks available balance
   v
Database
   |
   | if valid, creates leave_requests row
   v
API Backend
   |
   | starts approval workflow
   v
Agentspan
   |
   | returns execution_id
   v
API Backend
   |
   | stores execution_id on leave request
   v
Database
   |
   | leave request is pending_manager
   v
API Backend
   |
   | sends approval card to manager
   v
Slack Workspace
   |
   v
Manager receives approval request
```

## 5. Document Upload Flow

```text
Employee uploads document or image in Slack
   |
   v
Slack Workspace
   |
   | sends file event to API
   v
API Backend
   |
   | verifies Slack signature
   | maps slack_user_id to employee
   | downloads file from Slack
   | validates:
   | - file type
   | - file size
   | - whether a document is needed
   v
S3 / Document Storage
   |
   | stores file
   | returns document_key
   v
API Backend
   |
   | attaches document_key to:
   | - existing leave request, or
   | - current conversation session
   v
Database
   |
   | stores document_key
   v
API Backend
   |
   | continues leave request submission
   v
Slack Workspace
   |
   | confirms document was received
   v
Employee
```

## 6. Manager Approval Flow

```text
Manager receives approval card in Slack
   |
   | clicks Approve or Reject
   v
Slack Workspace
   |
   | sends interaction payload to API
   v
API Backend
   |
   | verifies Slack signature
   | extracts approver slack_user_id
   | finds approver in database
   | loads leave request
   v
Database
   |
   | returns approver + leave request + employee owner
   v
API Backend
   |
   | permission check:
   | employee.manager_id must equal approver.id
   |
   +-- if not allowed ---------------------------+
   |                                             |
   v                                             |
API rejects action                               |
   |                                             |
   v                                             |
Slack sends "not allowed" message                |

API Backend
   |
   | if allowed:
   | resumes Agentspan workflow
   v
Agentspan
   |
   | continues workflow after manager decision
   v
API Backend
   |
   | writes approval_events row
   v
Database
   |
   | manager decision is recorded
   v
API Backend
   |
   | if manager rejected:
   | - mark leave request rejected
   | - notify employee
   |
   | if manager approved and HR is not required:
   | - approve request
   | - deduct balance
   | - notify employee
   |
   | if manager approved and HR is required:
   | - mark request pending_hr
   | - send approval card to HR
```

## 7. HR Approval Flow

```text
HR receives approval card in Slack
   |
   | clicks Approve or Reject
   v
Slack Workspace
   |
   | sends interaction payload to API
   v
API Backend
   |
   | verifies Slack signature
   | extracts HR slack_user_id
   | finds HR employee record
   | loads leave request
   v
Database
   |
   | returns HR user + leave request
   v
API Backend
   |
   | permission check:
   | approver.role must be hr or admin
   |
   +-- if not allowed ---------------------------+
   |                                             |
   v                                             |
API rejects action                               |
   |                                             |
   v                                             |
Slack sends "not allowed" message                |

API Backend
   |
   | if allowed:
   | resumes Agentspan workflow
   v
Agentspan
   |
   | continues workflow after HR decision
   v
API Backend
   |
   | writes approval_events row
   v
Database
   |
   | HR decision is recorded
   v
API Backend
   |
   | if HR rejected:
   | - mark leave request rejected
   | - notify employee
   |
   | if HR approved:
   | - mark leave request approved
   | - write balance deduction
   | - notify employee
```

## 8. Balance Deduction Flow

```text
Final approval happens
   |
   v
API Backend
   |
   | calculates deduction:
   | - leave_type
   | - year
   | - days_requested
   v
Database
   |
   | inserts leave_balance_ledger row:
   | - employee_id
   | - leave_type
   | - year
   | - change_days = negative days requested
   | - reason = approved_leave
   | - leave_request_id
   v
Balance is reduced through ledger entry
```

Important:

```text
The system does not overwrite a single balance number.
It records balance movements.
Leave taken is calculated by summing approved leave deductions.
The employee-facing leave balance is the accumulated number of leave days taken.
```

## 9. Balance Check Flow

```text
Employee or manager asks for balance in Slack
   |
   v
Slack Workspace
   |
   | sends message/event to API
   v
API Backend
   |
   | verifies Slack signature
   | maps requester slack_user_id to employee
   | identifies whose balance is being requested
   v
Database
   |
   | returns requester and target employee
   v
API Backend
   |
   | permission check:
   |
   | allowed if:
   | - requester is checking own balance
   | - requester is target employee's manager
   | - requester is HR/admin
   |
   +-- if not allowed ---------------------------+
   |                                             |
   v                                             |
API denies request                               |
   |                                             |
   v                                             |
Slack sends "not allowed" message                |

API Backend
   |
   | if allowed:
   | sums leave_balance_ledger rows
   v
Database
   |
   | returns balance total
   v
API Backend
   |
   | formats balance response
   v
Slack Workspace
   |
   | sends balance to requester
   v
Employee / Manager / HR
```

## 10. Conversation Follow-Up Flow

```text
Employee sends incomplete leave request
Example: "I need sick leave"
   |
   v
API Backend
   |
   | LLM parser detects missing fields:
   | - start_date
   | - end_date
   | possibly document
   v
Database
   |
   | stores conversation_sessions row:
   | - slack_user_id
   | - current_intent
   | - collected_fields_json
   | - status = open
   v
API Backend
   |
   | asks next question in Slack:
   | "What start and end dates should I use?"
   v
Employee
   |
   | replies with dates
   v
API Backend
   |
   | loads open conversation session
   | merges new extracted fields
   | checks if request is complete
   |
   +-- if still incomplete: ask another question
   |
   +-- if complete: create leave request
```

## 11. Data Ownership

```text
Slack owns:
- Workspace identity
- User messages
- Button interactions
- File upload events

API owns:
- Business rules
- Permission checks
- Leave validation
- Workflow orchestration
- Slack response formatting

LLM owns:
- Parsing free-flow employee text into structured data
- Detecting missing fields
- Helping form clarification questions

Database owns:
- Employees
- Manager relationships
- Leave requests
- Balance ledger
- Approval events
- Conversation sessions
- Slack sync records

S3 / Document Storage owns:
- Uploaded documents
- Uploaded images
- Document retrieval URLs

Agentspan owns:
- Durable waiting for manager approval
- Durable waiting for HR approval
- Workflow resume state
```

## 12. Core Rule

```text
Slack is the interface.
The LLM understands language.
The API enforces rules.
The database stores truth.
S3 stores files.
Agentspan waits for humans.
```

## 13. ERD Diagram

```text
employees
---------
id PK
slack_user_id UNIQUE
email UNIQUE
name
role
department
manager_id FK -> employees.id
is_active
created_at
updated_at

Relationships:
employees.manager_id creates the manager/direct-report relationship.
One manager can have many employees.
One employee can have zero or one manager.
```

```text
employees
   1
   |
   | manager_id
   |
   0..many
employees
```

```text
leave_requests
--------------
id PK
employee_id FK -> employees.id
leave_type
start_date
end_date
days_requested
reason
document_key
status
agentspan_execution_id
created_at
decided_at

Relationships:
One employee can create many leave requests.
Each leave request belongs to exactly one employee.
```

```text
employees
   1
   |
   | employee_id
   |
   0..many
leave_requests
```

```text
leave_balance_ledger
--------------------
id PK
employee_id FK -> employees.id
leave_type
year
change_days
reason
leave_request_id FK -> leave_requests.id NULLABLE
created_at

Relationships:
One employee has many balance ledger entries.
One approved leave request can create one balance deduction entry.
Manual allocations or adjustments may not have a leave_request_id.
```

```text
employees
   1
   |
   | employee_id
   |
   0..many
leave_balance_ledger

leave_requests
   0..1
   |
   | leave_request_id
   |
   0..many
leave_balance_ledger
```

```text
approval_events
---------------
id PK
leave_request_id FK -> leave_requests.id
approver_id FK -> employees.id
approver_role
decision
comment
created_at

Relationships:
One leave request can have many approval events.
One employee can be the approver on many approval events.
Approval events provide the audit trail for manager and HR decisions.
```

```text
leave_requests
   1
   |
   | leave_request_id
   |
   0..many
approval_events

employees
   1
   |
   | approver_id
   |
   0..many
approval_events
```

```text
conversation_sessions
---------------------
id PK
slack_user_id
current_intent
collected_fields_json
status
updated_at

Relationships:
Conversation sessions are linked to Slack users by slack_user_id.
They temporarily store incomplete free-flow requests.
```

```text
employees
   0..1
   |
   | slack_user_id
   |
   0..many
conversation_sessions
```

```text
slack_sync_runs
---------------
id PK
status
users_seen
users_upserted
error
created_at

Relationships:
This table is operational history only.
It does not own employee records.
```

```text
manager_mappings
----------------
id PK
employee_email UNIQUE
manager_email
role
department

Relationships:
Manager mappings are imported from CSV.
They are applied to employees by matching employee_email and manager_email.
After application, employees.manager_id is the real manager relationship.
```

## 14. Full ERD Summary

```text
                         +----------------------+
                         |      employees       |
                         |----------------------|
                         | id PK                |
                         | slack_user_id UNIQUE |
                         | email UNIQUE         |
                         | name                 |
                         | role                 |
                         | department           |
                         | manager_id FK -------+----+
                         | is_active            |    |
                         +----------------------+    |
                                  |                  |
                                  | employee_id       |
                                  |                  |
             +--------------------+--------------------+
             |                    |                    |
             v                    v                    v
+----------------------+  +----------------------+  +----------------------+
|    leave_requests    |  | leave_balance_ledger |  |   approval_events   |
|----------------------|  |----------------------|  |----------------------|
| id PK                |  | id PK                |  | id PK                |
| employee_id FK       |  | employee_id FK       |  | leave_request_id FK  |
| leave_type           |  | leave_type           |  | approver_id FK       |
| start_date           |  | year                 |  | approver_role        |
| end_date             |  | change_days          |  | decision             |
| days_requested       |  | reason               |  | comment              |
| reason               |  | leave_request_id FK  |  | created_at           |
| document_key         |  | created_at           |  +----------------------+
| status               |  +----------------------+
| agentspan_execution_id|             ^
| created_at           |             |
| decided_at           |-------------+
+----------------------+

+-------------------------+       +----------------------+
| conversation_sessions   |       |   manager_mappings   |
|-------------------------|       |----------------------|
| id PK                   |       | id PK                |
| slack_user_id           |       | employee_email UNIQUE|
| current_intent          |       | manager_email        |
| collected_fields_json   |       | role                 |
| status                  |       | department           |
| updated_at              |       +----------------------+
+-------------------------+

+----------------------+
|   slack_sync_runs    |
|----------------------|
| id PK                |
| status               |
| users_seen           |
| users_upserted       |
| error                |
| created_at           |
+----------------------+
```
