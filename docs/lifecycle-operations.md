# Lifecycle operations API

This machine-only API lets a Paperclip routine read eligible external-customer
recipients and reserve/record sends. It does not use or impersonate a human
admin session.

## Authentication

All routes require `Authorization: Bearer <service token>`. The token is the
existing `PAPERCLIP_EVENTS_WEBHOOK_SECRET` secret injected by the deployment;
in AWS its credential path is the environment-scoped Secrets Manager secret
ending in `paperclip_events_webhook_secret`. Obtain access through the normal
platform secret grant. Never copy the value into code, logs, issues, or routine
configuration committed to Git.

## Candidate feed

`GET /api/v1/lifecycle/candidates?audience=marketing&limit=100&cursor=...`

`audience` is `marketing` (default) or `transactional`. The response is:

```json
{
  "candidates": [
    {
      "customer_id": "user_...",
      "workspace_id": "org_...",
      "email": "customer@example.com",
      "first_name": "Ada",
      "signup_at": "2026-01-01T00:00:00Z",
      "last_sign_in_at": "2026-01-02T00:00:00Z",
      "last_activity_at": "2026-01-03T00:00:00Z",
      "activation": {
        "workspace_created_at": "2026-01-01T01:00:00Z",
        "workspace_ready_at": "2026-01-01T02:00:00Z"
      },
      "plan": "free",
      "trial_ends_at": null,
      "subscription_status": "trial_local",
      "account_status": "active",
      "eligibility": {
        "transactional": true,
        "marketing": true,
        "ses_suppressed": false,
        "marketing_consented": true,
        "marketing_unsubscribed": false,
        "lifecycle_frequency_cap_hours": 72,
        "last_lifecycle_sent_at": null,
        "last_monthly_broadcast_at": null
      }
    }
  ],
  "cursor": null
}
```

The feed always excludes company-internal addresses. Marketing mode also
excludes missing consent, unsubscribe, SES bounce/complaint suppression, and
inactive/banned accounts. Consent is affirmative and canonical in the users
row as `marketing_email_consent_at`; unsubscribe is
`marketing_email_unsubscribed_at`. Transactional eligibility is separate and
does not treat marketing consent as transactional consent. SES failures fail
closed.

Follow `cursor` until it is `null`; an empty cohort is a successful response.

## Reserve a send

Before sending, call `POST /api/v1/lifecycle/ledger/claim`:

```json
{
  "recipient": "customer@example.com",
  "message": "trial-day-2",
  "eligibility_window": "2026-10-12",
  "kind": "lifecycle",
  "reason": "trial nurture"
}
```

A successful reservation returns `{"claimed":true,"ledger_id":"..."}`.
Otherwise it returns `{"claimed":false,"reason":"duplicate"}` or
`{"claimed":false,"reason":"frequency_cap"}`. Claim is atomic and
idempotent by normalized recipient + message + eligibility window. `lifecycle`
claims enforce at most one send per recipient in 72 hours. Set `kind` to
`monthly_broadcast` for a monthly broadcast; it has its own last-send field and
does not consume the lifecycle cap.

## Record the provider result

`POST /api/v1/lifecycle/ledger/{ledger_id}/result`:

```json
{
  "status": "sent",
  "provider_message_id": "provider-id",
  "sent_at": "2026-10-12T23:00:00Z",
  "failure": null
}
```

For failures, use `status: "failed"` and populate `failure`. The original
claim stores the reason, recipient hash, lifecycle message, eligibility window,
kind, and claim time. Recipient email is deliberately not persisted in ledger
rows.
