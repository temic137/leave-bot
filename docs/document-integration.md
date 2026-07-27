# Leave Document Integration

## Current Demo Flow

```text
Employee selects one PDF, JPG, or PNG in the Slack leave form
        |
        v
The API saves the request as draft and queues an upload job
        |
        v
The worker downloads the private Slack file with SLACK_BOT_TOKEN
        |
        v
The worker checks the type, file signature, and 900 KB limit
        |
        v
The worker uploads multipart field "file" to
POST https://api.staging.myautochek.com/document/upload
        |
        v
Autochek returns a public Google Cloud Storage URL
        |
        v
The URL is saved in leave_requests.document_key
        |
        v
The request moves to pending_manager and the manager is notified
```

If validation fails, the request becomes `cancelled` and the manager is not
notified. While upload is pending, `document_key` contains `slack:<file-id>`.

## Configuration

```text
AUTOCHEK_UPLOAD_URL=https://api.staging.myautochek.com/document/upload
AUTOCHEK_API_TOKEN=<secret>
AUTOCHEK_API_KEY=<secret>
AUTOCHEK_ALT_APP=marketplace_web_app
DOCUMENT_MAX_BYTES=900000
```

The Slack app needs the `files:read` bot scope. Slack accepts files up to 10 MB
in the modal, but this integration rejects files over 900 KB because the
Autochek staging gateway has returned HTTP 413 for larger multipart requests.

## Security Limit

The staging response marks its object as `public: true`. Sending the link only
to the assigned manager or HR controls who receives it, but does not make the
underlying object private.

Do not use this storage path for production medical or sensitive HR records
until the organization confirms:

- a private production upload endpoint;
- authenticated downloads or short-lived signed URLs;
- malware scanning;
- retention and deletion rules;
- audit logging and incident ownership.

The loan-specific `CreateDocuments` endpoint is not used. Leave documents are
stored but are not attached to an Autochek loan.
