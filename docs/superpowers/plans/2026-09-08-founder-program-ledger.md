# Founder OpenClaw upgrade execution ledger

Objective: the full user-submitted founder runtime and multiplayer goal, not merely a build or release-ready report.

## Binding sources

- Submitted goal: `/Users/prasiddhaparthsarthy/.codex/attachments/640a6762-e286-4999-b700-f101cd5bc125/pasted-text-1.txt`.
- Approved delivery specification: `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/docs/superpowers/plans/2026-09-08-openclaw-9x-founder-multiplayer-delivery-plan.md`.
- Preparation evidence: `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/docs/superpowers/plans/2026-09-08-openclaw-execution-readiness.md`.

## Authority

Implementation, isolated worktrees, tests, feature branches/PRs and verified nonproduction workflows are authorized. First production-affecting merge/deployment or founder image/config/state mutation requires an explicit release approval package. Other production owners and the new-user default remain unchanged. Retain all task ledgers and release evidence.

## Current execution state

- Goal active; this is its first execution turn (no prior goal turn to classify).
- Lightbulb base: `ba58f69b47514aca8ca9a47d400d3a40a8f91256`, fetched and checked against `origin/main` on 2026-09-08.
- Isolated branch/worktree: `codex/founder-openclaw-9x`, `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw-9x`.
- Main checkout has user-owned untracked files; untouched. `.worktrees` is ignored.
- AWS STS reverified account `877352799272` using explicit `isol8-admin`, `us-east-1`.
- Existing Desktop fork remains untouched. A documentation-claimed `/Users/prasiddhaparthsarthy/dev/agent-fork` does not exist. Clean fork checkout: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.worktrees/founder-openclaw-9x-fork`, branch `isol8/2026.9.3-founder`.
- Frozen official stable target: `v2026.9.3`, commit `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`, published 2026-09-08T14:15:53Z. Shipped fork `v2026.8.1-isol8.3` peels to `dfb72bf0d187a5d3d6ef35dd9cabcca5049bd31c`, 17 carried commits over upstream 8.1. Candidate fork is not yet patched, built, or published.
- Fork installation is blocked by its own seven-day package-age rule for pinned `pnpm@12.3.4` and matching platform packages. A narrow version-specific exception was requested from the user; no exception or safety override has been applied.
- Open PRs requiring coordination: #1922 provider-switch desired-zero reconciliation; #1913 BYO rotation; #2325 Slack pairing; #2400 orphaned Paperclip runs. No PRs merged or modified.
- Read-only reports completed in the original task's docs directory: `2026-09-08-fork-release-reconciliation.md`, `2026-09-08-founder-runtime-baseline.md`, `2026-09-08-lifecycle-reconciliation.md`, `2026-09-08-multiplayer-identity-seam.md`. Slack identity addendum is in progress.
- First implementation slice: schema vendoring source/provenance guard; baseline `5e42e4d7b20d5a737e334878ee920397c3ccfbf1`; implementer `schema_source_guard`; independent review required before acceptance. Plan and SDD ledger are retained.
- Baseline test command collected 292 version/schema/container-repository/update-service/ECS tests. Session 49850 remains live and progressing with no reported failures; final result not yet available.

### Founder read-only runtime snapshot

- Exact owner: `org_3H3viGAfq2SXc9dIdcatmGXg8Td`; ECS service `openclaw-org_3H3viGAfq2SXc9dIdcatmGXg8Td-c42489f74b24` in `lightbulb-prod-container`.
- At 2026-09-08T21:50Z the ECS-resolved live task was `7dcd7cea19b044a3a0f4bf35823234d6`, healthy on task definition `lightbulb-prod-openclaw-user:206`, image `2026.8.1-isol8.3-4e22eb8`, digest `sha256:c23039deb00f5293f3d2a3c905e7621177527e1fe97d0f7a334d564db515ba88`.
- DynamoDB cached a stopped Spot-interrupted task. Resolve live tasks from ECS again before any release operation; cached task/stop fields are not authoritative.
- EFS is mounted at `.openclaw`; nested `state` and `agents` are task-local volumes. Litestream replica prefix exists, but freshness/integrity/restore is unproven. EFS has no automatic backup policy/recovery points; replica S3 bucket has no versioning. Backup and isolated restore remain release gates.
- ECR tags are mutable. Qualify the final published/SOCI digest; do not infer immutability from workflow comments or locally built image IDs.

### Confirmed cross-cutting risks

- Size-only resize, stopped/silent reprovision, base-drift refresh, provider cold switch and plan/admin rebuilds all reuse the global image unless one desired-release resolver is threaded through their common builders.
- Current image update hot-writes target config before the old writer drains. Stopped reprovision can swallow config failure. Updates become applied before gateway health. None is acceptable for candidate activation.
- Native9.3 trusted-proxy rejects shared tokens; native password fallback is loopback-only. Current remote backend/Paperclip/node paths need demonstrated compatibility before human auth activation.
- Preserve current member-key privacy, backend authorization and service isolation. Native missing visibility defaults to shared, and membership is not a confidential-read ACL. Full session identity must replace agent-only panel identity for rooms/side sessions.
- Sharing the private report with task `01a082fb-5dad-7db2-8424-403b01cc9a93` was initially rejected by auto-review. The user explicitly approved that exact destination on 2026-09-08; handoff was then retried. This does not change production authority or program scope.

## Scope and proof matrix

| Requirement | Required authoritative proof | State |
|---|---|---|
| R0 target and baseline | Frozen upstream/fork refs; exact founder row/service/digest; state inventory and measurements | Source/runtime snapshot complete; restore inventory pending |
| R1 fork and Workshop | Carried-patch regression ledger; build; durable per-agent Workshop and local SQLite tests | Pending |
| R2 mixed releases | Persistent owner assignment; all lifecycle consumers; dual schemas; staged activation tests | Inventory complete; schema-source guard in progress |
| R3 dev qualification | Final image digest; clean/migrated boot; provider/plugin/node/Paperclip workflows; restore rehearsal | Pending |
| R4 founder rollout | Explicit approval; verified live release; replacement/lifecycle durability; representative observation | Pending approval package |
| F0 identity/privacy | Native per-human identity; service compatibility; private-history and revocation negative tests | Source seam documented; runtime prototype pending |
| F1 shared rooms | Two people, authorship/presence/typing/mentions, concurrency/reconnect; audience-safe client gating | Pending |
| F2 handoffs | Parent yields and receives one correct specialist result with Paperclip lineage across disconnect/restart | Pending |
| F3 side sessions | Persistent child routing/history/deep links without sibling/parent collisions | Pending |
| F4 devices/placement | Authorized stable device binding; worker ingress and recovery; no wrong-device execution | Pending |
| F5 Swarm/Code Mode | Explicit limits; nested spend/approval enforcement; cancellation and useful benchmark | Pending |
| F6 personal accounts | Member binding, consent, payer/fallback accuracy, secret isolation from transcripts/logs | Pending |
| F7 adjacent features | Workshop UX, public share/revocation, Slack stop/title, models/usage/recovery/diagrams/browser gates | Pending |
| Meeting library / Team Reports | Authorized sources, compatible dependencies, durable storage and audience-safe screens | Pending |
| F8 confidential/interorg | Concrete scoped designs and proof requirements; separate approval before new trust boundaries | Pending design; production excluded |
| Final acceptance | Full founder journey, platform availability, reviews, release evidence and recovery handoff | Not achieved |

## Decisions

- Ruling: reuse the already approved architectural direction and begin its runtime/config foundation without restarting broad brainstorming. The submitted goal explicitly authorizes ordinary implementation decisions; unresolved trust/privacy choices still require their stated gates. Cost if wrong: early foundation work may require revision, not unapproved production effects.
- Ruling: use the real Lightbulb repository's ignored worktree with scoped tool approvals from this active goal task. No native tool can move the calling task itself; creating another user task was not requested. Cost if wrong: scoped filesystem access may need the user's project-level adjustment before implementation can continue.

## Next action

Finish baseline tests and the schema-source guard with independent review. Resolve the pinned toolchain decision, port and qualify carried fork behavior, and implement persistent owner release selection with dual-schema/staged activation before exposing founder assignment. Do not mark the program complete until every required gate has direct evidence.
