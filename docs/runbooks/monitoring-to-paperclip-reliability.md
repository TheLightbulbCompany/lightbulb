# Monitoring to Paperclip reliability loop

Live path: Sentry webhook or CloudWatch/EventBridge adapter posts to `/api/v1/hooks/reliability/{sentry,cloudwatch}` with the shared reliability secret. The backend normalizes the signal, redacts secret-like fields, computes a stable dedup key, searches Paperclip for open matching issues, and creates a Jarvis-assigned issue only when no open match exists.

Controls:
- Fail-closed webhooks: `RELIABILITY_WEBHOOK_SECRET` must be configured.
- Sentry authenticity: optional `SENTRY_WEBHOOK_SECRET` validates `sentry-hook-signature` HMAC-SHA256.
- Paperclip routing: `RELIABILITY_PAPERCLIP_*` settings select API, company, parent issue, run id, and Jarvis ownership.
- Human gates: generated issues instruct Jarvis to investigate and prepare unmerged PRs; merge/deploy/high-risk remediation remains human-approved.
- Retry/dedup: issue descriptions carry `retryPolicy`, `dedupKey`, and provenance. Paperclip creation uses an idempotency key `reliability:{dedupKey}`.

Controlled test provenance for ISOA-122: on 2026-08-15 Jarvis sent a non-production CloudWatch-shaped signal (`iso122-nonprod-controlled-test`, account `000000000000`) through `ReliabilityPaperclipClient`; Paperclip created ISOA-124 with dedup key `6c6314251ade4e5b`. No production alarm, merge, deploy, or remediation was performed.

Access validation from the implementation run:
- GitHub auth: `gh auth status` succeeded for `Isol8Admin` with repo/workflow scopes.
- AWS auth: `aws sts get-caller-identity` succeeded for assumed role `isol8-connector` (account redacted in logs).
- Sentry: no `SENTRY_*` environment variables were present in the run container, so Sentry cannot be claimed live until deployed config is wired.
