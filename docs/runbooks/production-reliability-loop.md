# Production reliability loop

Status: implemented as the v1 policy/provenance contract plus operator runbook. This intentionally stops at an unmerged, reviewable artifact: humans still approve merges, deployments, destructive remediation, and high-risk incident handling.

## Flow

1. **Signal** — CloudWatch alarms (already defined in `apps/infra/lib/stacks/observability-stack.ts`) or a future Sentry issue emits a normalized `ReliabilitySignal`.
2. **Evidence bundle** — `core.services.reliability.loop.build_evidence_bundle` records severity, source, dedupe key, release, trace id, links to CloudWatch/Sentry/trace views, reproduction notes, customer impact, and redactions. It stores pointers and summaries, not raw logs or secrets.
3. **Severity/dedup policy** — `classify_severity` escalates secrets/revenue/broad impact to SEV1, repeated production errors to SEV2, low-volume production to SEV3, and non-production to SEV4. `dedupe_key` is `reliability:{source}:{environment}:{fingerprint}` before creating a Paperclip issue.
4. **Paperclip triage** — create/update a Paperclip issue with the evidence bundle. The assignee investigates and prepares an unmerged branch/PR.
5. **Review gates** — no autonomous merge or deploy. The bundle explicitly lists required human gates: merge, deploy, destructive remediation, and SEV1/secret handling.
6. **Deployment verification** — after human merge/deploy, verify the originating alarm/Sentry issue, release SHA, regression coverage, and customer impact before closing.
7. **Incident learning** — record root cause, regression test, rollback/forward-fix decision, and any policy changes on the issue/work product.

## Controlled test path

Use a non-production signal whose fingerprint starts with `controlled-test:`. The helper `is_controlled_test_bundle` only passes `local`, `test`, `dev`, or `staging` bundles with no redactions, so synthetic tests cannot accidentally validate a production destructive path.

Minimum provenance for acceptance:

- source and fingerprint
- environment and release/trace id when available
- CloudWatch/Sentry/trace links when available
- reproduction clue
- customer impact summary
- redaction notes, even when the note is “none”
- required human gates

## Current evidence and access notes

- GitHub CLI is authenticated in this workspace as `Isol8Admin` with repo/workflow scope.
- AWS credentials are present as assumed role `arn:aws:sts::877352799272:assumed-role/isol8-connector/isol8-org_3H3viGAfq2SXc9dIdcatmGXg8Td` in `us-east-1`.
- No Sentry SDK/CLI/runtime configuration is currently present in the repo outside credential-provider metadata, so Sentry is a future adapter rather than a validated live source in this PR.
- Existing CloudWatch alarm infrastructure is in `apps/infra/lib/stacks/observability-stack.ts`; this PR adds the safe policy/evidence layer those alarms can feed.
