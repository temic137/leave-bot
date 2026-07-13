# Deferred Document Integration

Document handling is intentionally deferred. This file preserves the agreed integration design so it can be implemented later without changing the leave-request workflow.

## Proposed Flow

```text
Employee attaches a PDF or image in Slack
        |
        v
Slack sends a file_share event to POST /slack/events
        |
        v
The API verifies the Slack signature and identifies the employee
        |
        v
The API downloads Slack's private file with SLACK_BOT_TOKEN
        |
        v
The API validates file type, size, and filename
        |
        v
The API uploads the file as multipart/form-data to
POST https://api.staging.myautochek.com/document/upload
        |
        v
Autochek returns a hosted fileUrl
        |
        v
The API stores the fileUrl as the leave request's document reference
        |
        v
An authorized manager or HR reviewer can open the document while reviewing
the leave request
```

## Autochek Upload Contract

```text
Method: POST
URL: https://api.staging.myautochek.com/document/upload
Content type: multipart/form-data
File field: file
Result used by leave bot: file.url
```

The documented bot-tool request containing `file_path` is not the HTTP request sent by the leave bot. The leave bot must download the Slack file and make a real multipart upload.

The loan-specific `Create documents` operation is not required. The leave bot needs hosted file storage, not attachment to an Autochek loan.

## Required Changes

1. Add or confirm the Slack bot scope `files:read`.
2. Accept Slack `file_share` events and file-only messages.
3. Download `url_private_download` with the Slack bot token.
4. Allow only agreed file types, initially PDF, JPEG, and PNG.
5. Enforce a maximum file size before downloading and uploading.
6. Add an Autochek-backed implementation of the existing `DocumentStorage` adapter.
7. Save the returned storage reference on `leave_requests.document_key`.
8. Add an authorized document link to manager and HR approval messages.
9. Add malware scanning, retention, deletion, and audit rules before production use.

## Decisions Still Required

- The authentication header or service credential required by the Autochek upload endpoint.
- Whether the staging endpoint may be used for the demonstration.
- Whether Autochek provides a production upload endpoint suitable for employee records.
- Whether uploaded URLs are public or private. The sample response contains `public: true`, which is not suitable for sensitive HR documents without explicit approval.
- The maximum file size and retention period.
- Whether production access uses private objects and short-lived signed URLs.

## Current Code Status

The policy can mark a leave type as requiring a document, and `leave_requests.document_key` can hold a storage reference. The end-to-end Slack download and remote upload path is not implemented. Until it is implemented, document-required leave should remain disabled for the live demonstration or be tested only with a manually supplied document reference.
