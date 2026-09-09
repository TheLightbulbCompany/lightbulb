# Autonomous multiplayer delivery — goal prompt

Prepared 2026-09-09. This is a ready-to-run prompt, not an active goal or a claim
that implementation has begun. The prompt starts below.

---

Build and deliver Lightbulb's approved native human-and-agent messaging product
autonomously using Subagent-Driven Development (SDD). Optimize for elapsed time,
complete usable outcomes, and a small number of coherent PRs. Stay in this task;
use implementation subagents, not new user-owned Codex tasks.

## Read once, then execute

Work in the Lightbulb repository. Read its current `CLAUDE.md`/`AGENTS.md`, then:

- `docs/superpowers/specs/2026-09-09-multiplayer-messaging-design.md`
- `docs/superpowers/plans/2026-09-09-multiplayer-messaging.md`

Use `superpowers:subagent-driven-development` and Impeccable for the UI. Preserve
the approved product requirements and visual brief; do not restart discovery or
ask me to reconfirm settled choices. This prompt controls execution and PR
packaging; the spec controls product behavior. Resolve ordinary reversible
implementation choices yourself and record consequential rulings with their risks.

Inspect current `main`, existing PRs, and the active runtime changes before work.
Use a `codex/` branch in an isolated worktree; preserve unrelated changes and
untracked `PRODUCT.md`/`output/`. Do not replay work already completed by another
agent. Never treat an old planning SHA or a release tag as deployed evidence.

## Deliver a real product, not a demonstration

Complete the core public/private rooms, human–human/human–agent/agent–agent DMs,
group DMs, threads, organization people-and-agent directory, verified authorship,
human-only messaging without inference, explicit bounded agent invocation,
presence/typing, reconnect recovery, unread/mentions, access-checked message
search, and contextual Paperclip tasks/artifacts/approvals.

Ship the complete web/Tauri core and implement Expo core parity using the same
qualified contracts. Two humans plus Friday/Atlas is the first integration
checkpoint, not the finish line. Attachments, reactions, edit/delete, notification
preferences/push (milestone G), guests, federation, WebRTC, a spatial canvas,
pricing changes, and a general runtime rewrite remain separate follow-on scope.

## Coordinate with the image/backend owner

I authorize sharing this plan and non-secret branch/PR, auth/backend, image, and
runtime-qualification details with the existing task **“Research OpenClaw 9.x
multiplayer AI”**, ID `01a082b2-b1d7-7c20-9521-74cff5c220ac`. Do not send secrets,
credentials, tokens, or customer conversations. Respect tool-enforced permissions.

At startup obtain its current PRs/branches, exact fork commit/image digest and
pins, native capability evidence, compatibility requirements, blockers, and file
ownership. It owns its runtime/image/auth changes; we own messaging product
integration and clients. Explicitly agree who edits shared transport, auth,
container configuration, and version-pin files. Never edit the other task's
checkout or duplicate its foundation work. Record the agreed contract and its
producing revision; refresh on contract changes and before final integration.

Exchange concise milestone updates and review findings at those boundaries.
Use compact task status/wait tools, not repeated full-history reads. A sent
message is not an acknowledged contract. If it has not replied, continue
independent implementation against clearly labeled fixtures; runtime enablement
and completion still require real evidence. If ownership is ambiguous, do not
race on the file. Work elsewhere while resolving the boundary.

## Fast SDD, not a PR factory

Target **two new Lightbulb product PRs**:

1. One complete backend/shared/web/Tauri messaging release, combining milestones
   B–E and our qualification integration from A. Consume the other task's existing
   reviewed image/backend work.
2. Expo core parity on the same contracts; do not hold a usable web release for
   the native-client delivery slice.

Use existing runtime/fork PRs where possible. Add a PR only for a real repository,
ownership, reviewability, or safe-deployment boundary, with the reason recorded.
Do not trade safety for an arbitrary count. Task commits and scoped reviews are
small; product PRs deliver complete outcomes. Batch useful commits into pushes,
watch every resulting run, and fix findings on the same PR. No PR per component,
test, cosmetic pass, or review fix.

Keep a durable SDD ledger of completed tasks, commits, contracts, file ownership,
test results, review findings, and remaining work. Resume from it after compaction.
Dispatch fresh implementers with bounded task briefs and explicit interfaces,
not the whole conversation. Batch related mechanical changes. Reuse the original
implementer for fixes; do not create a new agent for every finding. Workers do not
spawn their own reviewers or helpers.

Use one code-writing implementer at a time in a shared checkout. Parallelize
independent investigation, preparation, and reviews of frozen commit/diff
snapshots, while the external runtime task proceeds in its own workspace. Avoid
tests against a tree another worker is changing. Select available models by task
risk: efficient models for mechanical work, stronger judgment for UI, security,
concurrency, integration, and the final whole-branch review. Minimize redundant
context loading, repeated test runs on unchanged code, and idle polling.

Each meaningful task gets focused tests and an independent combined spec/quality
review. Fix material findings in batches; re-review the changed range and listed
findings. Perform a whole-integrated-branch review before merge. Respect the
SDD/Impeccable bounded review processes, but never waive a real security,
data-loss, authorization, or required-functionality defect to satisfy a loop cap.
Do not run several overlapping broad reviews of the same unchanged diff.

## Ground everything in the actual runtime

Reuse qualified native OpenClaw identity, history, viewing presence, typing,
mentions, and search before building substitutes. The spec pins inspected
upstream 2026.9.3 source; compare it with the actual fork candidate.

Explicitly prove non-inference human append, two simultaneous independently
authored messages while an agent is running, mixed-agent canonical room history,
and private-room isolation through agent tools/files/memory. Native `chat.send`
dispatch, synthetic `chat.inject`, sidebar visibility, and owner reassignment do
not establish those product guarantees. Preserve private legacy history. Keep
qualification fail-closed; never unlock features with fabricated profiles,
test-only maps, weakened auth, or a passing mocked contract alone.

Reuse `sessions.viewers.set` and native typing; supplement only proven missing
liveness evidence. Keep one authority per fact. Do not add a transcript mirror,
broker, vector database, framework, or speculative abstraction. A bounded fork
extension belongs with the runtime owner. If the native architecture requires a
broad rewrite or weaker security, stop that dependent work and present the
smallest evidence-backed architectural choice; continue unaffected work.

## Make the UI feel finished

Apply the Impeccable Operate brief: Linear-like hierarchy, Slack-like room/thread
navigation, Notion-like contextual detail, and Stripe-like semantic indicators,
expressed through Lightbulb's existing tokens, typography, and components.
Separate human presence, message persistence, agent execution, and approvals.
Keep tool chatter collapsed and the composer stable. Implement empty, loading,
working, approval, reconnect, uncertain-send, and access-removed states, not just
the populated happy path. No fabricated progress, audience, or availability.
Preserve keyboard access, focus, mobile navigation, reduced motion, and readable
non-color status labels. Batch visual inspection and fixes rather than polishing
indefinitely or redesigning unrelated surfaces.

## Verify, review, and deliver

Run focused tests during development. After integrating the current image/backend
candidate and all final fixes, run the full affected test/typecheck/lint/build
gates, native-runtime integration, legacy/auth/billing/operator/node regressions,
and the spec's two-user end-to-end scenarios. Exercise reconnect, duplicates,
revocation, concurrent authorship, exhausted AI credits, private-context leakage,
and two backend processes. Run the specified pilot load/freshness measurements.
Inspect desktop/narrow UI, Tauri, and Expo lifecycle; record exact commands,
results, candidate commits, and image digests. Skipped or mocked runtime tests
are not release evidence. Regenerate/commit CDK workflow changes when required.

You may implement, commit, push feature branches, open/update PRs, address reviews,
and merge in-scope changes after required checks and reviews pass. Follow the
repository's `claude-review` gate and Codex second-opinion process; never bypass
branch protection. Watch CI and the dev deployment, then validate the actual
deployed candidate. Do not modify production hosts, perform destructive resets,
change billing, or publish a production/native release without explicit release
authority. Inspect merge-triggered workflows first: if a merge would trigger a
production OTA or another gated release, hold that merge for authorization.

The goal is complete when the full core messaging PR is merged and verified on
dev, Expo parity is implemented/tested/reviewed and merged or explicitly held
release-ready at its production-release gate, and all required implementation,
review, and test findings are resolved. Production promotion and native-store
publication are separate human-gated actions. Do not call a mocked demo, disabled
shell, green PR with missing runtime proof, or unverified deployment complete.

Continue autonomously between tasks; do not ask “should I continue?” Report
milestones, consequential decisions, and actionable blockers concisely. Ask only
for missing authority, destructive/external actions, a material product/security
tradeoff, or an indispensable external dependency. At handoff give PR links,
exactly what is merged/deployed/release-ready, tests and runtime evidence,
coordination status, significant rulings, and the remaining release gates. Delete
shipped disposable planning material only when its scope is finished and its
durable decisions/evidence have moved to the PR record or living documentation.
