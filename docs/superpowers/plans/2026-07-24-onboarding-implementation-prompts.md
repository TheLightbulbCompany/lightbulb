# Onboarding Rebuild — Implementation Prompt-Pack

**For:** running the #1406 architecture as parallel Claude Code sessions.
**Spec:** `docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md` (read first).
**Research:** `docs/superpowers/research/2026-07-24-onboarding-current-state.md`.
**Disposable:** delete this file once the sequence ships (per CLAUDE.md plan-disposal policy).

## Decisions locked (2026-07-24)

- **Two generations**, both approved. Onboarding is **company-fenced** (`onboarding_generation`); runtime is **container-fenced** (`provision_generation`). First-run triggers on first container spin-up; a container *replacement* under a surviving company (reprovision, provider change, crash) does **not** re-onboard.
- **Step Functions** for provisioning — adopt PR #1388 de-flagged.
- **Org-level onboarding, once** — done by the org admin; a seat-join is not a generation bump and does not re-onboard. Org and personal have **separate funnels**.
- **Orgs have no free tier** (`trial_local` is personal-only, `user_provision.py:63-84`) — orgs must pick a provider first; `provider_selection_required` is an explicit contract state.
- **Personal→org conversion cleanup (old L6): out of scope** — no live path in the main app today.
- **`_purge_builtin_agents`: delete it**, paired with a CDK test pinning `PAPERCLIP_SKIP_BUILTIN_AGENTS` + a `roster_drift` metric.
- **No feature flags.** Active on merge; validate in dev; rollback = `git revert`.

## How to use

Each block below is one Claude Code session. **Prepend the shared preamble** to each. Run sessions within the same wave concurrently; a wave starts once its dependency PRs are merged. Peak parallelism is **5 sessions (Wave 1)**, settling to **2–3 sustained** because the onboarding backbone (GEN → TABLE → COMPOSER) is a serial spine while SFN, the fork PRs, and the client fan-out run alongside it.

### Wave map

| Wave | Sessions (parallel) | Depends on |
|---|---|---|
| 1 | GEN · FORK1 · FORK2 · TRIGGER-L3 · F1 | — |
| 2 | ONBOARD-TABLE · SFN | GEN |
| 3 | COMPOSER | ONBOARD-TABLE |
| 4 | BOOTSTRAP · REALTIME · CLIENT-HOOK | COMPOSER |
| 5 | SWEEP | BOOTSTRAP |
| 6 | FORK-ADOPT | FORK1, FORK2 (pins promoted) + ONBOARD-TABLE |
| 7 | CLEANUP | FORK-ADOPT + SFN |

---

## SHARED PREAMBLE (prepend to every prompt)

```
You are implementing one PR of the Lightbulb AI onboarding rebuild (GitHub #1406). Before writing code, read in full:
- docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md (the architecture)
- docs/superpowers/research/2026-07-24-onboarding-current-state.md (why; file:line current-state)
Rules: NO feature flags — your code is active on merge; validate in dev; rollback is git revert. Red-first/TDD for behavior changes. Small, reviewable PR scoped to ONLY the task below. Match surrounding code style. Keep exactly one owner per fact — do not add a second store or a client-side reconciliation for durable truth. When you touch a compensation layer the spec marks for deletion, delete it in this PR (don't leave it dark). Commit signed, push to a feature branch, open a PR that references #1406 and states what it deletes. Uphold the spec's invariants (INV-1..INV-12); if your change would violate one, stop and flag it.
```

---

## WAVE 1

### Session `GEN` — Two generation ids (foundational, inert)
```
TASK (spec PR-3): Add the two generation identifiers, additive and inert (no consumer reads them yet).
- Add `provision_generation` (ULID) to the containers table/row. Mint it at EXACTLY ONE point: inside create_user_service's row-creating write (event G1). It must be PRESERVED by scale-from-zero, force-redeploy, restart, provider hot-swap, Bedrock revoke/restore, and Stripe conversion — those reuse the same row. Verify no other write path mints or overwrites it.
- Add a `bump_onboarding_generation` helper + the onboarding_generation ULID concept (unused by callers yet — the table itself lands in ONBOARD-TABLE; here just the id-minting helpers and the containers column).
- Tests: a fresh dev signup gets a provision_generation; a restart/redeploy does NOT change it; a fresh service create does. Assert the single-mint-point (INV-2).
This PR changes no behavior and deletes nothing. It unblocks ONBOARD-TABLE and SFN.
```

### Session `FORK1` — Paperclip: honor Idempotency-Key (fork repo)
```
REPO: Isol8AI/paperclip (the fork; NOT this monorepo). Work on the lightbulb/2026.7-circuit-breaker line; ship via its own CI/tag, then bump paperclip-version.json's pin in this repo (dev then prod) in a follow-up.
TASK (spec PR-1 / FORK-1): Make company-create and agent-create honor Idempotency-Key: store key→response for a bounded TTL and replay the stored response on a repeat, instead of minting a duplicate. Today the key is read exactly once server-wide (an unrelated webhook route) — verified against 2fdafe83.
- Tests: two create calls with the same Idempotency-Key return the SAME company/agent (replay), not two rows.
This unblocks deleting this repo's reconcile-adopt / Atlas-skip / 409-as-success family (later, in FORK-ADOPT). Do NOT touch this repo's compensations here.
```

### Session `FORK2` — Paperclip: hard user-delete/reset (fork repo)
```
REPO: Isol8AI/paperclip (the fork). Same ship-via-CI-then-pin flow as FORK1.
TASK (spec PR-2 / FORK-2): Add a hard user-delete/reset endpoint (Better-Auth admin delete/reset) so an owner's account can be truly removed. No such API exists today (verified by exhaustive route grep @ 2fdafe83).
- Tests: delete-then-re-signup with the same email SUCCEEDS (no 422 duplicate-email dead-end).
This unblocks killing this repo's adopt-via-signin, 422-terminal classification, recover_orphan_paperclip_member.py, and the raw Aurora DELETE FROM "user" (later, in FORK-ADOPT).
```

### Session `TRIGGER-L3` — One trigger family + L3 org guard
```
TASK (spec PR-12): (a) Fold the undocumented second autoprovision door at paperclip_proxy.py:918-957 (keyed on user_id) into the canonical owner_id-keyed ensure_user_ready, so exactly one autoprovision trigger family exists (INV-11); delete the second door. (b) Add `and existing.org_id == owner_id` to paperclip_provisioning.py:306 so a seat-only ex-member re-entering personal context never adopts the org's company_id (L3 own-side defense; cross-tenant deletion is already fenced by Paperclip authz per the research verification, this is defense-in-depth + prevents the failed-row re-point that drops the 30-day purge timer).
- Tests: the proxy path routes through the canonical trigger; a disabled cross-org row does not reconcile.
Independent of all other PRs.
```

### Session `F1` — Stop-first provider change (fixes F1)
```
TASK (spec PR-11): Fix the cold provider-change no-op. change_provider's cold path (billing_service.py ~1187) currently dispatches background_provision, whose lease REFUSES running rows, so the switch silently stands down while returning restart:True. Change it to call ecs_manager.reprovision_for_user (stop_user_service → _wait_for_service_drained → background_provision) — mirroring the admin reprovision path — so the running container is actually rebuilt with the new provider. A bare lease-takeover is NOT enough: it lands on the forceNewDeployment branch (ecs_manager.py:2333-2371) which rewrites nothing.
- Merge gate: apps/backend/tests/unit/services/test_change_provider_cold_running_container.py must flip red→green (it currently pins the buggy stand-down).
- Verify: oauth→byo on a running container actually switches the live provider; the funnel is NOT re-shown (this bumps provision_generation but preserves onboarding_generation once those consumers exist).
Deletes the silent-refuse no-op path. Composes with GEN but the core fix is independent.
```

---

## WAVE 2 (after GEN)

### Session `ONBOARD-TABLE` — onboarding fields on paperclip-companies + G2 + writer-safety + atomic wipe (fixes L1)
```
TASK (spec PR-4): Make the paperclip-companies OWNER ROW the single backend-owned onboarding source of truth. NO new table (owner decision 2026-07-24) — fold onto the existing paperclip-companies row we own in DDB.
- Add onboarding ATTRIBUTES to the paperclip-companies owner row via paperclip_repo: onboarding_generation, funnel_state (not_started|submitted|complete), funnel_submitted_at, funnel_completed_at, onboarding_goal_id, onboarding_project_id, bootstrap_state (not_started|dispatched|delivered|failed), bootstrap_dispatch_id, bootstrap_dispatched_at, bootstrap_delivered_at, bootstrap_attempts, bootstrap_last_error. (company_id + friday_agent_id already exist there.) DDB is schemaless — no CDK table to create.
- All onboarding writes go through generation-conditioned UpdateItems (a write against a superseded onboarding_generation is a silent no-op — INV-4). CRITICAL SAFETY REQUIREMENT: audit every existing whole-item repo.put on this row (provision_org's reconcile-adopt / status-flip puts) and make them ONBOARDING-PRESERVING — convert to a scoped UpdateItem, or read-merge the onboarding attrs before the put. If you skip this, reconcile clobbers onboarding state (the exact bug we're avoiding). This is the hardest part — do it carefully and test it.
- Mint onboarding_generation at EXACTLY ONE point: provision_org's fresh-create branch, AFTER a genuinely-new Paperclip company is confirmed (G2). NOT on reconcile-adopt of an existing company_id, NOT on provision_member (org seat) — INV-3. The mint atomically sets the new generation + funnel_state=not_started + bootstrap_state=not_started on the same write that records the fresh company.
- Cut POST /onboarding/brief to claim + write funnel_state via a conditional write (funnel_state: not_started→submitted, fenced on onboarding_generation — the loser gets ConditionalCheckFailed = already submitted; this REPLACES the __onboarded__ sentinel's one-shot seed claim + 60s stale-steal) plus goal/project ids. Keep org vs personal funnels distinct (personal has the trial path, org does not).
- DELETE containers.onboarded_at (S2) writers+readers and the paperclip __onboarded__ sentinel (S3) claim/record/release entirely (superseded by funnel_state on the row). Fixes L1 by construction (onboarding now lives on a company-fenced row a container recreation never touches).
- Ship the atomic-wipe admin/debug endpoint that deletes the container row + the Paperclip Aurora company + the paperclip-companies owner row (onboarding rides with it) together (INV-10); retire/redirect the old debug DELETE doors.
- Tests: reprovision-with-company-preserved → funnel stays complete, no re-prompt (L1); a reconcile-adopt write does NOT clobber funnel/bootstrap (the writer-safety test — this is the one that proves co-location is safe); company-recreated → funnel resets; concurrent /brief → exactly one wins the claim; atomic wipe leaves zero orphan.
Depends on GEN. Blocks COMPOSER/BOOTSTRAP/SWEEP.
```

### Session `SFN` — PROVISION Step Functions, de-flagged (fixes L2)
```
TASK (spec PR-10 + §7): Land the reshaped PR #1388 as the live provisioning executor. Pull #1388's branch as the starting point.
- Remove the PROVISION_EXECUTOR flag and the inline default entirely — no dual-run window (no users, active-on-merge). Two provisioning systems must never be live at once, so pull the three synchronous doors (/provision, /retry, /recover) and the doors' supersede-deletion into THIS PR.
- Close #1388's two open hazards HERE: (1) mint a fresh per-retry attempt token while keeping the execution name stable (fences straggler ECS/Bedrock side effects, preserves Branch-A idempotency); (2) hand the Branch-B closed-without-terminal-write case to the reconciliation_worker via DescribeExecution (do the worker hook now or stub the query if SWEEP hasn't landed — coordinate).
- Thread provision_generation (from GEN) through provision_runner's command line; condition every provisioning DDB write on it. Convert the containers write path to field-scoped generation-conditioned UpdateItems (NO whole-item put) and surface/classify the swallowed describe_services error at ecs_manager.py:1751-1753 instead of diverting to the full-provision branch — this is the L2 fix (SFN becomes the sole writer of the running/error transition).
- DELETE: _await_running_transition's unbounded detached loop + every asyncio.create_task of it; resolve_running_container's poll-driven auto-transition; the stale-lease provisioning-recovery guard; the boot-reconciler re-arm; in-process org/recovery locks superseded by StartExecution idempotency.
- VALIDATION: unit tests + ASL/CDK synth assertions in CI. NO LocalStack, NO dev-soak gate, NO flag — we turn Step Functions on in real dev and iterate (owner decision). Just make sure the state machine synthesizes, the runner's exit-code ABI + retry classification are unit-tested, and the concurrent-StartExecution idempotency (one execution per owner) is covered by a unit test.
Depends on GEN. Big; runs parallel to the onboarding backbone.
```

---

## WAVE 3 (after ONBOARD-TABLE)

### Session `COMPOSER` — compute_status() + StatusContract (fixes L4)
```
TASK (spec PR-5): Build the single readiness composer and the client-rendered contract.
- New core/services/readiness_state.py with compute_status(owner_id) as the SOLE reader of the five inputs: containers row, billing-accounts row, gateway liveness, paperclip-companies teams_state, and the generation-fenced onboarding fields on the paperclip-companies row. No router/hook/gate re-derives readiness (INV-1) — enforce with an import-linter/architecture test asserting no other module imports >1 of the five stores.
- MOVE gateway liveness OFF the in-process pool onto the connection-service DDB table (kills readiness store R4 and deletes PR #1364's self-heal dial).
- Emit one StatusContract (shared type in packages/shared) + deriveUIPhase. The lifecycle enum MUST include `provider_selection_required` (orgs have no free tier — they must pick a provider before provisioning; personal owners get trial_local). Chat availability is decided SOLELY by runtime phase, never by onboarding status (INV-6).
- Rewire GET /container/status, resolve_teams_context, and useOnboardingGate's backend check onto compute_status. Rewire ProvisioningStepper + the mobile equivalent to RENDER deriveUIPhase (fixes L4 by construction — one shared implementation, no mobile latch).
- DELETE the ProvisioningStepper useMemo phase ladder + the byte-forked mobile classifier.
- Tests: every lifecycle scenario (blocked, provider_selection_required, team-pending, cold-start, error, parked, ready) renders correctly on web AND mobile from one implementation; a degraded/awaiting-brief status still yields a live chat surface.
Depends on ONBOARD-TABLE. Blocks BOOTSTRAP/REALTIME/CLIENT-HOOK.
```

---

## WAVE 4 (after COMPOSER)

### Session `BOOTSTRAP` — Server-owned bootstrap dispatch + client cutover (fixes L5, ATOMIC)
```
TASK (spec PR-6): Move Friday's first-run kickoff server-side. THIS MUST BE ONE ATOMIC PR — shipping the server dispatcher and the client cutover separately leaves a window where the client latch and the server dispatcher each win a claim.
- Backend dispatcher: when funnel is submitted AND runtime is up, claim pending→dispatched with a fresh bootstrap_dispatch_id (conditional write, only the claimer sends — INV-5), send chat.send(FIRST_RUN_PROMPT) over the pooled connection, and observe chat.final → bootstrap_state=delivered. All fenced to onboarding_generation. If the runtime isn't up at brief-submit, WAIT for the next lifecycle→onboarding transition — do NOT store a stale handoff token.
- In the SAME PR, DELETE: useFirstRunKickoff (web + mobile), the Clerk firstRun.started/completed writers, and the #1391 thread-emptiness reconcile. Stop seeding BOOTSTRAP.md (it's inert on the pinned OpenClaw fork — see research §5).
- Tests: submit a brief with NO tab open → Friday's thread receives and answers the kickoff → bootstrap_state converges to delivered → reopening shows it via chat.history.
Depends on COMPOSER (contract) + ONBOARD-TABLE (bootstrap fields). Coordinate the DescribeExecution/worker hook with SWEEP.
```

### Session `REALTIME` — Realtime invalidation + two free event wins
```
TASK (spec PR-7): Add a publish_status_changed helper wired into the ~6-8 durable write sites (containers / billing / paperclip-companies (incl. onboarding) transitions); the client subscribes once and mutates the shared status key on the event. Ride along two cheap wins the research found: wire config.changed (raw-forwarded to every browser today, consumed by nobody — one mapping line gives all config.get mounts real push freshness) and restore useGatewayRpc's severed health event→method mapping. Also fix runToastReducer's missing `interrupted` case (a terminal status published on graceful BFF shutdown → currently a stuck Infinity-duration toast; one line).
- Test: a backend-side transition reflects in an open tab within one event round trip, no poll tick.
Depends on COMPOSER. No structural deletions (latency + one-line bug fix).
```

### Session `CLIENT-HOOK` — One hook, one bounded loop (web + mobile)
```
TASK (spec PR-9): Collapse the client freshness stack. Land useOnboardingStatus (single SWR key over the StatusContract) + ONE raw setInterval backstop (survives Chrome occlusion-throttling — the research proved the real kill vector is a hung fetcher / rejected revalidate, not a dropped tick, so ALSO add a fetch timeout/AbortController to the status fetcher and a rejection-safe loop). Visibility-gated; cadence 5s(<60s)/15s/off-at-ready. Every current consumer becomes a pure reader. Port the SAME hook to mobile (parity by construction — fixes L4's mobile half; include a mobile re-onboard-after-reprovision regression test).
- DELETE: ProvisioningStepper's 3s poll + revalidateOnFocus + 1s watchdog; useSystemHealth's own poll; OverviewPanel's focus opt-in; the wake/unpark/container_waking mutate trio; the briefBaselineRef/sessionBriefSubmittedRef flash-bridge; the mobile polling/classifier copy.
- Test: simulate occlusion-throttling (kill the SWR chain) → the bounded loop still converges within its declared ceiling; mobile re-onboards after a reprovision.
Depends on COMPOSER.
```

---

## WAVE 5 (after BOOTSTRAP)

### Session `SWEEP` — Generalized reconciliation worker
```
TASK (spec PR-8): One core/services/reconciliation_worker.py on a single 60s cadence with GSI-backed needs-attention queries (INV-9): stale provision lease, conversion-pending guard elapsed, bootstrap dispatched-but-undelivered (retry via bootstrap_dispatch_id fencing, bounded attempts), trial-idle, the SFN closed-without-terminal-write check (DescribeExecution — coordinate with SFN), AND **provider-change drift** (billing-account provider_choice != the provider actually applied to the container config/task-def → re-apply via reprovision). The provider-drift reconcile is the SYSTEMATIC fix for the F1 partial-apply hole — surfaced by the #1412 Codex P1 review: if a cold provider change commits set_provider_choice but the stop/reprovision fails midway, billing says new-provider while the container runs old-provider, and the request-path retry no-ops. This worker detects and heals that drift generation-fenced, so F1's request path only needs a minimal best-effort attempt, not a bulletproof one.
- DELETE the boot-only provisioning-resume reconciler (_resume_provisioning_transitions), maybe_resume_stuck_conversion, and the standalone trial-idle sweep.
- Test: kill the backend mid-provision AND mid-bootstrap-dispatch → the next worker tick (not the next client poll) resolves both.
Depends on BOOTSTRAP (+ coordinates with SFN).
```

---

## WAVE 6 (after FORK1 + FORK2 pins promoted + ONBOARD-TABLE)

### Session `FORK-ADOPT` — Delete the fork-gap compensations (fixes L7)
```
TASK (spec PR-13): Once the FORK1/FORK2 pins are promoted (paperclip-version.json dev+prod), collapse the two compensation families. VERIFY each deletion against the pinned fork source (/workspace/paperclip @ the promoted commit) before removing it.
- SeedCompany passes Idempotency-Key = onboarding_generation; DELETE the reconcile-adopt branch, the Atlas-skip-on-reconcile carry, and the 409-as-success tolerance.
- DELETE adopt-via-signin, the 422-duplicate-email terminal classification, recover_orphan_paperclip_member.py, and the raw Aurora DELETE FROM "user" in account-wipe (use the FORK-2 endpoint) — fixes L7.
- Tests: concurrent provision_org calls for one owner mint exactly one company/Friday/Atlas via idempotency replay (not reconcile-adopt); a full wipe leaves zero orphaned Paperclip rows; re-signup on a wiped email succeeds.
Depends on FORK1+FORK2 (promoted) + ONBOARD-TABLE.
```

---

## WAVE 7 (after FORK-ADOPT + SFN)

### Session `CLEANUP` — Purge deletion, coverage, close #1388
```
TASK (spec PR-14, adjusted): 
- DELETE _purge_builtin_agents entirely (obsolete — the fork skip-builtin-agents gate covers company-create + startup reconcile, confirmed against pinned source). Replace the safety net: add a CDK test asserting PAPERCLIP_SKIP_BUILTIN_AGENTS is set on the Paperclip container, and a roster_drift metric (list_agents vs seeded_agent_ids post-provision) so a fork-gate regression is detected.
- Add the workspace-state.json round-trip CI check: assert this repo's seeded JSON (js_iso_now format) round-trips through the pinned OpenClaw fork's parseSetup validator — guards the PR #1394 failure class (a bad timestamp makes every agent turn throw).
- Formally close the PR #1388 branch; delete its plan/spec docs (git history + PR-close comment are the durable record per CLAUDE.md).
- Delete THIS prompt-pack and the two superpowers docs' now-shipped status once the sequence is fully merged.
Depends on FORK-ADOPT (purge) + SFN (close #1388).
```

---

## Not in this sequence (deferred / out of scope)

- **Personal→org conversion cleanup** (old L6) — no live path in the main app; revisit when one exists.
- **Per-member org first-run** — org-level onboarding is sufficient (owner decision); a member-scoped record is a separate feature if ever needed.
- **#1409 full remediation** — its two roots ship as FORK1/FORK2/FORK-ADOPT here; the broader defensive-code cleanup + the reportsTo-blast-radius / agent-hires-orphan findings are the #1409 spec (P2), authored separately.
