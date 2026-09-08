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
- First implementation slice accepted: schema vendoring source/provenance guard, commit `a11402079ea61c8c8bd2a23fcb631cb105b16d48`; implementer `schema_source_guard`, independent `schema_source_review` spec compliant/quality approved/no findings. Controller fresh focused verification15passed, exit0. Plan/report/review/SDD ledger retained; no artifacts or release pins changed.
- Baseline session49850 finished:292 version/schema/container-repository/update-service/ECS test assertions passed in1501.75s. Coverage teardown could not write its worktree SQLite data file, so command exit3 is NOT a clean verification result. Correct the output-path environment and obtain clean final validation after the next fix.
- Stopped-service required-config failure refusal accepted at `afb5e1f92790dd50d8aa5954ff4d055bbdad6eec`: 5 focused and 37 broader tests green, independent spec/quality review approved without findings. The existing outer fenced error handler retains original classification. One writer remains the rule.
- Coverage-output diagnosis: default pytest coverage writes into the worktree outside this task's write roots. A27-test version-suite smoke with `COVERAGE_FILE` and JSON output under `/private/tmp/lightbulb-founder-coverage.ACBgGi` passed with coverage, exit0. The final broad post-fix run is still due; no coverage safety setting changed.
- Chrome CLI1.8.0 works with scoped local-browser escalation. Reopened default test profile navigates to production sign-in, not an authenticated app. User was asked to sign in directly again; production auth must be reverified before dependent UI/release work. Do not treat earlier logged-in evidence as current.
- New retained reports: Slack identity/onboarding addendum and native multi-node contract. A handoff of new private findings to `New voice chat` was rejected as outside the first report's specific approval; broader related-handoff approval is pending. No retry of rejected private content until authorized.
- Related-sharing approval subsequently verified by reading the latest user turn in `New voice chat`: user explicitly agreed to the upgrade task sharing additional Slack/node findings. The bounded report and links were handed off with native-versus-planned and untested limitations; no secrets or unrelated material included.
- Dev reserved test-user authentication reverified in memory using the authorized test-only Clerk key and supported single-use token. Correct user `user_3IsMkvZ1E3rKIeC0WJX8lqzPgFh`, no active organization. No trial/provider/onboarding test is claimed from that login.
- Native machine-auth qualification retained: frozen9.3 device/bootstrap paths exist and the relay forwards signed client auth verbatim. Unattributable ingress blocks fallback; roles configured require a verified identity for every operator, including backend/Paperclip. Node-role credentials are separate. Tauri currently fails to retain native hello credentials; actual packaged CLI behavior remains unverified. Test a distinct, nonhuman native proxy principal and bounded node credential enrollment before considering a bridge. No auth mode switched or compatibility test claimed.
- Approved related-task handoff sent the machine-auth qualification and limits to `New voice chat`; production authority and program scope unchanged.
- Broad post-fix regression run at code commit `afb5e1f92790dd50d8aa5954ff4d055bbdad6eec` started as session29406. Uses full coverage with outputs under `/private/tmp/lightbulb-founder-coverage.ACBgGi`, replacing the prior unwritable-output failure. Pending result.
- Next independently testable slice: explicit schema selection/fail-closed artifact handling in existing config-schema module; legacy callers unchanged. Candidate artifact remains absent/unregistered until a qualified fork build. Separate read-only design resolves cross-update/provision/config fencing before release assignment implementation.

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
| R2 mixed releases | Persistent owner assignment; all lifecycle consumers; dual schemas; staged activation tests | Source guard and stopped-config failure gate accepted; explicit schema selector next; full release transaction pending |
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

- Product clarification relayed from the user's voice task: one shared conversation must support work across multiple authorized native OpenClaw nodes, including concurrent work on teammates' nodes. A one-node probe is only a prerequisite, not feature completion. Native explicit per-invocation targeting and separately placed native execution children are the source-supported direction; exact grant/placement lifecycle integration and failure proofs remain unimplemented. No generic scheduler/mesh or extra conversation containers.
- Slack-first onboarding is an optional proposed entry path, not a mandatory separate-web-signup flow. Reuse native human profiles with verified Slack identity and bounded authorized membership/seat admission; do not autolink names/emails or confuse model-auth profiles with people. Official Clerk create-user/direct-membership APIs exist, but instance policy, consent, billing and Paperclip readiness must be proved before activation.

- Ruling: reuse the already approved architectural direction and begin its runtime/config foundation without restarting broad brainstorming. The submitted goal explicitly authorizes ordinary implementation decisions; unresolved trust/privacy choices still require their stated gates. Cost if wrong: early foundation work may require revision, not unapproved production effects.
- Ruling: use the real Lightbulb repository's ignored worktree with scoped tool approvals from this active goal task. No native tool can move the calling task itself; creating another user task was not requested. Cost if wrong: scoped filesystem access may need the user's project-level adjustment before implementation can continue.

## Next action

Finish the broad post-fix coverage run, implement/review explicit schema selection, and resolve the pinned toolchain decision. Port and qualify carried fork behavior, then wire persistent owner release selection with dual-schema/staged activation before exposing founder assignment. Do not mark the program complete until every required gate has direct evidence.
