# How The Leave Bot Works

This document explains the leave bot in simple words.

## 1. The Main Parts

```text
Employee or Manager
        |
        | runs a command, submits a form, or clicks a button
        v
      Slack
        |
        | sends the structured information to the bot
        v
   Leave Bot API
        |
        +------> FastEmbed
        |        Chooses a fixed action from a normal Slack message
        |
        +------> Leave Database
        |        Saves Slack links, approvals, policies, jobs, and audit history
        |
        +------> Performance API
        |        Supplies eligibility and current balances
        |        Receives new requests and status changes
        |
        +------> AgentSpan
        |        Keeps track of the approval steps
        |
        +------> S3 Bucket (later)
                 Will store medical notes and other files
```

### Slack

Slack is where people talk to the bot.

It gives the bot the Slack ID of the person sending the message. The bot uses
that ID to find the correct employee in the database.

### Leave Bot API

The API is the main part of the system. It:

- receives commands, forms, and button clicks from Slack;
- checks who sent the message;
- uses FastEmbed to match normal messages to fixed actions;
- checks the selected leave type and dates;
- reads and writes information in the database;
- starts approval work in AgentSpan; and
- sends replies back to Slack.

### Leave Database

PostgreSQL is the bot's main storage. It remembers employees, managers, leave
requests, policies, approval decisions, and HR balance adjustments.

### AgentSpan

AgentSpan remembers which approval step comes next.

```text
Normal leave: Employee -> Manager -> Finished

HR leave:     Employee -> Manager -> HR -> Finished
```

AgentSpan does not store employee records or calculate days taken.

### FastEmbed

FastEmbed runs MiniLM inside the API. It can choose actions such as request
leave, check balance, view history, check status, or help. It does not generate
answers, read policy rules, extract dates, or approve leave.

### Document Storage

Slack receives the file. A background job downloads it and sends it to the
configured organization document service. PostgreSQL stores the returned file
URL, not the file itself.

## 2. When An Employee Requests Leave

```text
1. The employee runs /leave-request in Slack.
                    |
                    v
2. The bot opens a form for leave type, dates, reason, and document reference.
                    |
                    v
3. The employee submits the form.
                    |
                    v
4. The API checks that the form really came from Slack.
                    |
                    v
5. The API finds the employee using their Slack ID.
                    |
                    v
6. The API checks the dates, leave policy, document rule, and manager.
                    |
        +-----------+-----------+
        |                       |
   Something is wrong      Everything is valid
        |                       |
        v                       v
7A. Slack shows the       7B. The request is saved.
    error in the form.            |
                                  v
                            8. AgentSpan starts the approval process.
                                  |
                                  v
9. The manager receives Approve and Reject buttons.
```

No generative LLM is used. Slack gives the API structured values, so the API
does not need to guess dates or policy information.

If the employee starts with a normal message, FastEmbed chooses an action. The
bot explains what the matching button will do before showing it. If the match is
unclear, the bot explains and shows all available action buttons.

## 3. Cancelling Leave

```text
Pending request:
Employee chooses Cancel -> request is cancelled immediately

Approved request:
Employee requests cancellation -> manager receives a cancellation card
                                  -> manager approves or keeps the leave
```

The old approval card is updated to show the latest status. Its buttons are
removed so the same decision cannot be clicked again.

## 4. When A Manager Approves

```text
1. The manager clicks Approve in Slack.
                    |
                    v
2. Slack sends the button click to the API.
                    |
                    v
3. The API checks that this person is the employee's manager.
                    |
                    v
4. AgentSpan records that the manager step is finished.
                    |
          +---------+---------+
          |                   |
     HR is not needed     HR is needed
          |                   |
          v                   v
5A. Request becomes      5B. Request goes to HR
    approved.                for another decision.
          |                   |
          +---------+---------+
                    |
                    v
6. The decision is saved and the employee is told in Slack.
```

If the manager clicks **Reject**, the request becomes rejected and the employee
is told in Slack.

## 4. Checking Leave Balance

In live mode, a row in the balance API means the employee is eligible for that
leave type. Its `balance` value is the employee's current remaining days.

```text
Employee: Show my leave balance.
                    |
                    v
The API reads the employee's current balance rows.
                    |
                    v
The API adds approved request days to calculate days used.
                    |
                    v
The bot shows allocated, used, and remaining days.
```

For example, an API balance of 14 with 6 approved days is displayed as 20
allocated, 6 used, and 14 remaining. Pending requests reserve days while they
wait for a decision.

## 5. Employees And Managers

Every person has one row in the `employees` table.

```text
Chetan (manager)
    id = 1

Temi (employee)
    id = 2
    manager_id = 1
```

The `manager_id` on Temi's row points to Chetan's row. This tells the bot that
Chetan is Temi's manager.

The manager can be added before or after the employee. If both people already
exist, the admin edits the employee and selects the manager. The admin should
not create a second employee with the same Slack ID.

## 6. Editing A Leave Policy

The admin writes the policy as normal text:

```text
Annual Leave: 20 days maximum. No document required. Manager approval only.
Sick Leave: 10 days maximum. Document required. Manager approval only.
Maternity Leave: 90 days maximum. Document required. HR approval required.
```

When the admin saves it:

```text
Policy text
    |
    v
The API checks the text
    |
    v
A new policy version is saved
    |
    v
The newest version becomes the policy the bot uses
```

Old versions are kept so changes can be checked later.

## 7. Simple Database Diagram

```text
employees
  - Who the person is
  - Their Slack ID
  - Their external employee ID
  - Their country
  - Their role
  - Who their manager is
       |
       | one employee can have many requests
       v
leave_requests
  - Leave type
  - Start and end dates
  - Number of days
  - Current status
  - External request ID and leave type
  - Slack messages that need to be updated
       |
       | one request can have many decisions
       v
approval_events
  - Who approved, rejected, cancelled, or overrode
  - Their role
  - Their decision
  - When it happened

leave_balance_adjustments
  - Employee and leave type
  - Year
  - Days added or removed by HR
  - Reason and HR user


leave_policy_versions
  - The policy text
  - Version number
  - When it was saved

durable_jobs
  - Work that still needs to be done or retried
```

## 8. How The Tables Connect

```text
employees.manager_id
    points to another employee who is the manager

leave_requests.employee_id
    points to the employee who asked for leave

approval_events.leave_request_id
    points to the leave request being approved or rejected

approval_events.approver_id
    points to the manager or HR person who made the decision

leave_balance_adjustments.employee_id
    points to the employee whose allocation changed

leave_balance_adjustments.adjusted_by_id
    points to the HR or admin user who made the change
```

## 9. What Each Table Is For

| Table | Simple meaning |
|---|---|
| `employees` | People, Slack IDs, roles, and manager links. |
| `leave_requests` | Every leave request and its current result. |
| `approval_events` | A history of who approved or rejected each request. |
| `leave_balance_adjustments` | Audited days added to or removed from an employee's allocation. |
| `leave_policy_versions` | The current policy and all older versions. |
| `durable_jobs` | Work that must survive a restart and may need to be tried again. |

## 10. What Happens When Something Breaks

### The API restarts

Saved requests are not lost. Unfinished jobs remain in `durable_jobs` and the
worker continues them after the API starts again.

### Slack sends the same message twice

The job has a unique key. The duplicate is ignored, so the bot does not create
the same request twice.

### Slack cannot receive a reply

The reply job waits and tries again.

### AgentSpan is unavailable

The AgentSpan job waits and tries again. The saved leave request is not lost.

### A job keeps failing

After the retry limit, the job is marked `dead`. It stays in the database so the
problem can be inspected instead of disappearing.

## 11. Who Is Allowed To Do What

```text
Employee
  - Ask for leave
  - See their own requests
  - See their own days taken

Manager
  - Do everything an employee can do
  - See days taken for direct reports
  - Approve or reject requests from direct reports

HR
  - See employee leave information
  - Handle requests that need HR approval

Admin
  - Add and edit employees
  - Connect employees to managers
  - Edit leave policies
  - View policy history
```

The API checks these rules. A person does not receive manager or HR access just
because they are a member of the Slack workspace.

## 12. What Is Finished And What Is Later

Working now:

- Slack commands and leave-request forms;
- employee and manager records;
- leave requests;
- manager approval buttons;
- HR approval steps;
- policy editing and policy history;
- external employee, balance, and leave-request integration;
- allocated, used, and remaining balance totals;
- database storage, retries, and restart recovery; and
- AgentSpan approval tracking.

Still to be completed later:

- uploading Slack files to S3;
- secure document viewing and deletion rules;
- choosing one specific HR approver instead of notifying all active HR/admin users;
- cancelling or reversing an approved request; and
- automatic alerts when a job has permanently failed.
