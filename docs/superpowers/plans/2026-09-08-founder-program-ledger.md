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
- Existing Desktop fork remains untouched. A documentation-claimed `/Users/prasiddhaparthsarthy/dev/agent-fork` does not exist; a clean verified fork checkout is required.
- Open PRs requiring coordination: #1922 provider-switch desired-zero reconciliation; #1913 BYO rotation; #2325 Slack pairing; #2400 orphaned Paperclip runs. No PRs merged or modified.
- Read-only subagents: `fork_release_reconcile` (stable target/patches/plugins), `lifecycle_inventory` (all release/config consumers).

## Scope and proof matrix

| Requirement | Required authoritative proof | State |
|---|---|---|
| R0 target and baseline | Frozen upstream/fork refs; exact founder row/service/digest; state inventory and measurements | Reconciling |
| R1 fork and Workshop | Carried-patch regression ledger; build; durable per-agent Workshop and local SQLite tests | Pending |
| R2 mixed releases | Persistent owner assignment; all lifecycle consumers; dual schemas; staged activation tests | Inventory in progress |
| R3 dev qualification | Final image digest; clean/migrated boot; provider/plugin/node/Paperclip workflows; restore rehearsal | Pending |
| R4 founder rollout | Explicit approval; verified live release; replacement/lifecycle durability; representative observation | Pending approval package |
| F0 identity/privacy | Native per-human identity; service compatibility; private-history and revocation negative tests | Pending |
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

Complete baseline tests and source reconciliation; freeze the upstream release; write the first concrete, self-reviewed implementation plan and execute with independent task review. Do not mark the program complete until every required gate has direct evidence.
