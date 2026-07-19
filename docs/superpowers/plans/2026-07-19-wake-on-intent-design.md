# Wake-on-intent gating — verified design (workflow wf_75682474-bd5, 2026-07-19)

Produced by a 14-agent map/design/adversarial-verify workflow. All three candidate designs FAILED verification as written; this is the surviving hybrid. Delete this doc when the implementation PR ships.

---

# Wake-gating: final recommendation

## 1. What to build: a hybrid — Design 2's verb model + Design 3's server-side legacy backstop

**No design survived verification as written** (all six verdicts: `survives: false`), but the failures sort cleanly into (a) fatal-to-one-design mechanisms and (b) shared implementation bugs every design must fix. The hybrid keeps the mechanisms that verification proved sound and drops the ones it proved broken.

**From Design 2 (explicit-wake-endpoint), keep:**
- **The query-param suppress marker (`?wake=0`), not a header.** This is the single most important verified decision. Both verifiers of Designs 1 and 3 independently confirmed the same hard break: `main.py:676` `allow_headers` is an explicit list, the app is cross-origin with credentials, so any custom header makes every stamped request fail CORS preflight whenever new-FE meets old-BE (frontend-first deploy, or any later backend rollback — Vercel ships in ~2-5 min vs ECS in ~10-20 min, so a single-PR ship *guarantees* the outage window). A query param appended inside fetchers (SWR key unchanged) is order-independent in both directions and makes backend code rollback safe. This kills the entire deploy-ordering break class at the design level instead of via deploy discipline.
- **`POST /container/wake`** as the explicit intent verb, backed by a shared `core/services/container_wake.py` so all dispatch sites use one lease-idempotent path.
- **Server-side wake in `chat.send`'s stopped branch** (`websocket_chat.py:~1905-1937`). Verification of Design 1 proved the client-delegated wake is structurally fragile: the one `swrMutate` that IS the wake computes visibility at fetch time, so Cmd-Tab during the WS round trip suppresses the promised wake and deadlocks "Waking your agent…" with hidden-tab polling paused. Moving the wake server-side makes message-send intent wake-proof for every client, including shipped mobile binaries.

**From Design 3 (server-wake-budget), keep:**
- **`stop_reason="trial_idle"` + `parked_at` stamping** in `stop_user_service` (reaper passes the reason; admin/killswitch/reprovision stops overwrite it). This gives both doors a precise, symmetric predicate — fixing Design 1's verified feed-door break where crashed *paid* boxes and error rows got falsely labeled parked and lost their only poll-driven heal.
- **A DDB-backed daily budget (default 6/UTC-day) on legacy poll-driven wakes** of `trial_idle`-parked rows. This is the only mechanism any design offered that bounds the *actual live incident tab* — Design 2's verifier proved client-pull adoption cannot remediate it ("long-lived open tabs and desktop windows never reload spontaneously… keeps waking its box for days-to-weeks after ship, and the design provides NO server-side lever"). The budget takes effect the moment the backend deploys, no client update required.
- **The scoped `update_item` discipline for the debit write** (never `update_fields` — its full-item get→put racing `try_claim_provision` resurrects `status="stopped"` over a claimed lease, the documented double-provision class).

**Drop (verification-fatal):**
- **Design 3's cooldown + arrival-bypass + in-memory per-owner poll-gap map.** Both verifiers broke it the same way: the map is keyed per-owner, so the incident's own idle desktop tab (10s health poll) masks the gap and classifies a mobile app-open as "continuous," suppressing the design's load-bearing old-mobile guarantee for up to 10 minutes — and the suppressed response's 5s mobile re-poll makes the misclassification self-sustaining. Budget-only for legacy is simpler, has no cross-device state, and Design 3 itself conceded "the budget, not the cooldown, is the real fence."
- **Design 1's tri-state header intent** (CORS trap + visibility-conjunct races).
- **The `gate_cleared` auto-wake trigger.** Both Design 2 verifiers proved it re-creates the incident verbatim: the UTC-midnight cap restore is a blocked→normal transition observed by an idle visible tab, so every capped owner's open tab fires a zero-interaction wake at midnight, fleet-synchronized — directly contradicting the design's own flow-handling promise. Post-restore, the box wakes on the next real interaction, full stop.
- **The `pendingWakeResendRef` auto-resend, deferred out of v1.** Three independent verified break classes: the ref lives inside the stepper's unmount boundary and is destroyed by the very mutate the design keeps; manual-resend-during-wait double-sends (two runs, double cap burn); and sends during the 60-90s provisioning window take the un-designed `"starting up"` error branch (the wake's lease claim flips the row to `provisioning` within ms, and `container_waking` fires only on `status=="stopped"`). Keep today's calm copy + manual resend; auto-resend becomes a follow-up with explicit requirements (state outside the unmount boundary, clear-on-manual-send, session pinning, TTL).

**Fix everywhere (the shared break that hit all three designs): parked must be a first-class server phase.** `container.py:51-69` maps any non-`running` status to phase `"provisioning"`, and `ProvisioningStepper.tsx:647` renders children only when derived phase is `"ready"` — so a suppressed 200 replaces the *entire* chat/dock surface with a frozen, eventually-"timed out" cold-start stepper (verified against the real render gate by four of six verdicts). The suppressed response must return `phase: "parked"`, and every phase consumer must be swept, not just the two Design 1 listed (its verifier found the missed `useGetStartedDerived.ts:44` equality check).

## 2. Surviving break-risks and mitigations

| Verified break (source) | Hybrid mitigation |
|---|---|
| CORS preflight outage on deploy skew (D1×2, D3) | Eliminated structurally: query param, no new header, no CORS change. `POST /wake` 404 from an old backend is a per-request soft-fail, never cached (explicit test). |
| Parked renders as frozen full-screen stepper / eternal cold-start (D1, D2×2, D3) | Server returns `phase:"parked"` only to `wake=0` clients. Stepper treats `parked` as ready-equivalent on **both** sides of the regression guard — latch `reachedReady` on `ready\|parked` AND treat `previousBackendPhaseRef ∈ (ready, parked)` as ready (fixes D1's prev-phase escape at `ProvisioningStepper.tsx:372-380`). Sweep: `useGetStartedDerived` (`phase ready\|parked`), `useSystemHealth` (new `PARKED`, `canRecover=false`), `HealthIndicator` (exhaustive map), `TeamsDockPanel`. |
| Pre-warm wake unmounts composer mid-typing, destroying draft (D1 verifier 2) | New stepper behavior: when the transition into `provisioning` comes **from** `parked`/`ready` (i.e., a wake, not a first provision), keep children mounted and show a slim non-blocking "Waking your agent" banner instead of the full-surface swap. First-provision onboarding keeps the full stepper. |
| Teams door swallows the capped/expired 402 (D2 verifier 1) | Ordering is law: in `feed.py`, evaluate the provision gate (the `_self_heal_reconcile` gate path producing the 402) **before** any parked short-circuit. Only when gates are clear and the row is `stopped + trial_idle + trial_local + !conversion_pending` (the *same* `wake_policy` predicate as the status door — fixes D1's asymmetry) does the parked 200 apply. Converting rows (`conversion_pending`) are excluded → they keep today's 202 overlay. |
| Teams stuck on "asleep" after out-of-band wake (D2 verifier 2) | Parked is a **200 with real Paperclip-backed dashboard data + `workspace_state:"parked"`** (Design 3's shape), never a terminal 202 error state. Panels render content with a banner + Wake button and keep a slow 30s revalidation, so a chat-driven wake self-clears without event plumbing. |
| `chat.send` server wake races conversion reprovision into the EFS-WAL two-task window (D3 verifier 1) | `request_wake_silent` stands down on **`conversion_pending`** (the durable flag), not merely the 300s `conversion_guard_active` window — the exact gap the verifier found. The `container_waking` frame still emits; the follow-up poll's conversion machinery owns the row. |
| Midnight/gate-cleared non-intent wake (D2×2) | No `gate_cleared` trigger exists. `requestWake` requires `visible AND interaction < 120s` for `mount`/`interaction` reasons; `message_send`/`retry`/`teams_wake` bypass recency (they *are* the interaction) but are user-initiated by construction. |
| Old-bundle budget-exhausted UX: eternal stepper on fresh load, CONTAINER_DOWN on latched tabs (D3 verifier 2) | Accepted, bounded, and documented: appears only after 6 poll-wakes in one UTC day on a stale bundle; escape hatches always work (chat send wakes server-side budget-free; Restart CTA is an ungated intent door). Population shrinks with every Vercel deploy. |
| Cross-device arrival masking (D3×2) | Eliminated: no cooldown, no poll-gap map. Mobile app-open wakes whenever budget remains; when exhausted, chat send still wakes. |
| Telemetry blind spots: escape doors uninstrumented, per-poll suppressed spam, legacy unattributable (D1 v2, D3 v2) | `container.wake.dispatch{door,intent}` emitted at **every** wake door including `recover`, `provision`, `retry`, `admin`; `rewake_gap` from a shared helper on any parked-row start regardless of door; `suppressed{door,reason}` deduped to once per (owner, park episode) via in-memory last-emitted cache; dispatch log line carries a coarse UA class for legacy attribution. |
| Kill switch honesty (D1 v2, D3 v2) | `TRIAL_WAKE_GATE_ENABLED` documented as a CDK deploy (~15-25 min), and **every** public `wake_policy` entry point (poll decision, feed peek, budget check) returns ALLOW/no-op when off — closing D3's spec gap. `stop_reason` stamping and the `chat.send` wake run unconditionally (harmless; keeps re-enable coherent). Because there is no header, backend code revert is also safe — the flag is preferred but not the only safe lever. |
| Admin blindness to the new dominant fleet state (D3 v2) | `stop_reason`/`parked_at` ARE persisted (unlike response-only `parked`); admin per-user view and `StatusChip` render `stopped + trial_idle` as "parked (asleep)", and `wake_debits` shows in the per-user overview. |

## 3. Implementation plan

### API shape
- `GET /container/status?wake=0` — suppresses only the `stopped + trial_idle + trial_local + !conversion_pending` dispatch in the auto-retry else-arm (`container.py:305-315`); response gains additive `parked: true`, `phase: "parked"`, `wake_retry_after_s`. Everything else on the route byte-identical: seat sync, no-row signup dispatch, `maybe_restore_trial_bedrock` (:221), `bedrock_revoked` 402 (:233), `maybe_resume_stuck_conversion` (:259), trial 402s (:291-297), **error-row and stale-lease self-heal stay ungated**, org reconcile (:328).
- `GET /container/status` (param absent, legacy) — wake-on-read preserved, but debited; over budget → same suppressed dispatch, response unchanged in shape (`status:"stopped"`, no new fields — old bundles get today's field set).
- `POST /container/wake {reason: mount|interaction|message_send|retry|teams_wake}` → `{outcome: dispatched|already_running|in_flight|converting|not_owner|no_row_provisioning}`; 402 with gate payload when blocked. Bypasses the budget (explicit intent).
- `GET /teams/*?wake=0` — dashboard returns 200 + `workspace_state:"parked"` (gates evaluated first); `resolve_teams_context`/`_provision_after_gate` downgrade to `provision_container=False` under suppress (Paperclip repair still runs).

### Backend (deploy 1 — safe alone, fixes the live incident on its own via the budget)
1. `core/services/wake_policy.py` (NEW): the single suppress predicate (used by both doors), budget check/debit, `intent` resolution from `wake` param + `POST /wake`; every entry point flag-guarded.
2. `core/services/container_wake.py` (NEW): `request_wake` / `request_wake_silent`; stands down on `conversion_pending`; org-aware no-row path mirroring `container.py:148-197` exactly (D3 verifier's caller-drift warning); idempotent via the provision lease, no status pre-write.
3. `core/repositories/container_repo.py`: `record_wake_debit` via scoped `update_item` `SET wake_debits = :v` — **never** `update_fields`.
4. `core/containers/ecs_manager.py`: `stop_user_service(*, reason="manual")` folds `stop_reason` (+ `parked_at` when `trial_idle`) into the existing single row write; callers updated (`connection_pool.py:_maybe_stop_trial_container` → `trial_idle`, killswitch → `killswitch`, conversion drain → `reprovision`).
5. `routers/container.py`: `POST /wake`; status route reads `wake` param; suppressed branch + parked phase; legacy budget path.
6. `routers/websocket_chat.py`: stopped branch dispatches `request_wake_silent` before the unchanged `container_waking` frame; update the :1898-1908 invariant comment ("WS path never wakes" is now false — flag it in the PR and CLAUDE.md/teardown lore).
7. `routers/teams/feed.py` + `agents.py` + `deps.py`: gate-first ordering, parked 200, `allow_container_start` downgrade.
8. `core/config.py`: `TRIAL_WAKE_GATE_ENABLED=True`, `TRIAL_WAKE_DAILY_BUDGET=6`.
9. Admin: surface `stop_reason`/`parked_at`/`wake_debits` in the per-user view.

### Frontend (deploy 2 — order-independent)
10. `hooks/useProvisioningState.ts` + `hooks/useTeamsApi.ts`: append `wake=0` inside the fetchers (SWR keys unchanged); expose `parked` / `workspace_state`.
11. `lib/interactionRecency.ts` (NEW) + `hooks/useContainerWake.ts` (NEW): `requestWake(reason)` with in-flight dedupe, 20s cooldown, recency gate for passive reasons, 404 soft-fail per request (never cached).
12. Triggers: mount-with-visible in `WorkspaceShell`/`ChatLayout` (guard the `/chat` breakpoint-remount refire with the cooldown); shell interaction tracker (`pointerdown`+`keydown` only — no `touchstart`, per the verified mobile-scroll-swipe leak); `useAgentChat.sendMessage` when phase ≠ ready; stepper Retry/Check-again; Teams Wake button. **No visibility-flip trigger, no gate_cleared trigger.**
13. Stepper: parked = ready-equivalent (both current- and prev-phase sides); wake-from-park renders inline waking banner, children stay mounted. `useSystemHealth` PARKED, `HealthIndicator` entry, `useGetStartedDerived` `ready|parked`, `TeamsDockPanel` parked banner.
14. `apps/infra/openclaw/skills/isol8` SKILL.md: one-line free-tier update (containers park when idle, wake on interaction).

### Tests
- `tests/unit/services/test_wake_policy.py`: full predicate matrix (paid / error / stale-lease / conversion_pending / non-trial_idle stops / flag-off / billing-read fail-open), budget rollover, dedup of suppressed emission.
- Router matrix: suppressed shape + no dispatch; legacy debit + over-budget suppress with legacy-shaped response; **gate ordering** (capped owner gets 402 on both doors, never parked); conversion resume untouched; error/stale-lease dispatch unchanged; org no-row parity of `POST /wake` vs status route.
- `chat.send`: dispatch on stopped; stand-down on `conversion_pending` and on trial gate (frame still emitted).
- Frontend (single-file vitest runs): stepper parked pass-through incl. prev-phase guard; waking-banner keeps children mounted; `requestWake` recency/cooldown/404-soft-fail; `useTeamsApi` parked kind with 30s revalidation; GetStarted/health mappings.
- Repo test for `record_wake_debit` proving it never rewrites `status`.

### Telemetry
`container.wake.dispatch{door: status_poll|status_legacy|teams|chat_send|wake_endpoint|recover|provision|retry|admin, intent}`, `container.wake.suppressed{door, reason: budget|gated}` (once per park episode), `container.wake.rewake_gap` (shared helper, all doors — success = p50 moves from ~28s to hours), `container.wake.blocked{gate}`, plus the recommended pre-existing-gap alarm `billing.trial_conversion.stuck` (conversion_pending > 15 min). Observability-stack widget pairing background/legacy dispatches against trial stops **in the change list this time**. PostHog: `container_wake_requested{reason,outcome}`, `container_parked_viewed`.

### Rollout
1. Land backend PR; deploy to **dev**; soak: park a dev trial account, leave an idle tab open, confirm `suppressed`/legacy-debit metrics and that the tab stops re-waking past 6; verify conversion self-heal by stamping `conversion_pending` and watching the poll resume it; verify capped-owner 402s on both doors.
2. Deploy backend to prod. **The budget bounds the live incident tab immediately** — before any frontend ships.
3. Land frontend PR (Vercel auto-deploy). Fresh loads get full gating + parked UX; desktop webview inherits on next reload; stale bundles remain legacy (budget-bounded) until refresh.
4. Watch `dispatch{intent:legacy}` decay; mobile EAS follow-up (append `wake=0`, `POST /wake` on foreground/send) is a separate PR, no urgency — legacy semantics are safe by design.
5. Break-glass: flip `TRIAL_WAKE_GATE_ENABLED=false` via CDK (~15-25 min, honestly stated); backend code revert is also safe (no header dependency).

## 4. What this does NOT solve

- **Holding, only re-waking.** Once an interaction wakes the box, the tab's live WS vetoes scale-down indefinitely (teardown untouched by scope). A desktop window left open after one click holds a box up for its lifetime — ~$0.02/hr Fargate once heartbeat `every:"0m"` ships; that PR, not this one, is what makes an up-but-idle box token-free.
- **Legacy wake-on-read is bounded (≤6/day/owner), not eliminated.** Eliminating it would brick shipped mobile binaries' app-open wake. Mobile stays analytics-dark; legacy volume never reaches literal zero, so any future polarity flip is a deliberate product decision (accepting mobile degrading to send-to-wake), not a telemetry threshold.
- **First-message cold-start friction remains.** Manual resend after "Waking your agent" (today's gap, kept deliberately after auto-resend failed verification three ways); sends landing in the 60-90s provisioning window still get the plain "starting up" error, and multi-sends during the wait are not queued. Follow-up work with explicit requirements listed above.
- **Not a security or abuse fence.** Intent is client-asserted; any authed script can `POST /wake` — same trust level as today's `POST /provision`. This is cost/thrash hygiene.
- **Budget is approximate (±1-2)** under multi-task last-writer-wins debit races; acceptable for a cost bound.
- **Pre-existing gaps left open, flagged for separate work:** the `error`-row poll self-heal is still an unbounded retry loop (now the largest remaining non-intent wake path); no alarm when conversion self-heal never runs (metric recommended above); Teams work dispatched at a parked box still silently queues in Paperclip until wake — the parked banner mitigates visibility but a teams-side "will run when awake" notice is a follow-up.
- **Day-1 economics unchanged for paid owners** — always-on by design; nothing here touches them (verified: every suppress predicate is `trial_local`-scoped and `stop_reason`-scoped).
