# Mobile Onboarding Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each Slice below is an independent, shippable unit with its own PR — execute one at a time, in order.

**Goal:** Bring web's onboarding capabilities to the React Native mobile app — free-tier showcase, in-app inference-provider sign-in (ChatGPT OAuth + BYO key), and the goal/project funnel for all owners — by extracting shared logic into `@isol8/shared`, with **zero backend changes** and purchases staying as external web handoffs.

**Architecture:** The backend provisioning/onboarding/OAuth path is already client-agnostic (verified — keys only on the Clerk `AuthContext`). Web and mobile both consume the same `GET /container/status` gate and the same `@isol8/shared` API client. This plan extracts the *logic* (not UI) into `@isol8/shared` hooks that take an injected `api: ApiMethods`, matching the existing `useGatewayRpc` precedent, and gives mobile native UI over that shared logic. Web is rewired behind 2-line shims so its behavior is byte-for-byte unchanged.

**Tech Stack:** React Native / Expo 57, React 19.2.3 (single deduped instance across both apps), `@isol8/shared` (TS source, no build step), `expo-web-browser`, Clerk (`@clerk/clerk-expo`), Vitest (shared/web), jest-expo (mobile).

## Global Constraints

- **ZERO backend changes.** The provisioning gate, `/container/status`, `/oauth/chatgpt/*`, and `/onboarding/brief` are all client-agnostic and already serve mobile. Do not modify `apps/backend/**`. (Verified: `provision_gate.py` Layer 1 subscription → `trial_local` short-circuit → Layer 2 provider; `container.py:444` already emits `onboarded`.)
- **Purchases stay EXTERNAL.** The $50/mo subscription (Layer 1, required for any paid provider) and Bedrock-managed credits are *purchases* → Apple Guideline 3.1.1 → keep the existing `Linking.openURL` handoff to isol8.co. Do NOT build in-app Stripe/IAP checkout. Provider **sign-in** (ChatGPT OAuth, BYO key) is account-linking, not a purchase → in-app is fine.
- **Subscription-before-provider ordering is backend-enforced and inherited** — do not replicate any sequencing client-side; render whatever gate `/container/status` returns.
- **Ship as a NEW BINARY, not OTA.** These are new features; expo-updates OTA is for bug fixes only (App Review 3.2/2.5).
- **App Review prerequisites (before submission, not per-task):** demo account with active sub + connected provider; in-app account deletion (Clerk); external-billing presentation confirmed against the *current* 3.1.1(a) disclosure rules at submit time.
- **`@isol8/shared` files must never contain `"use client"`** (Next-only; inert but forbidden in shared) or import from `@/lib/api` / `@clerk/nextjs` (web-only). Shared hooks take the client as a parameter.
- **React version parity:** shared declares `react` as a *peerDependency* (`>=19`); both apps are on `19.2.3`. Never add `react` as a direct dependency of shared.
- **Do not rename the SWR key string `"/container/status"`** — web cross-revalidates by that literal in `useAgentChat`/`useContainerWake`; a rename silently breaks cross-revalidation.

---

## Verified foundation (grounding for a zero-context engineer)

These facts were verified by direct code read (workflow `wf_de895cfd-877`). Trust them; re-confirm the file:line before editing.

1. **Shared-hook injection precedent exists:** `apps/frontend/src/hooks/useGatewayRpc.ts:23-29` calls `useGatewayRpcShared(useGateway(), …)`. Mobile runs shared hooks (`useGatewayConnection`, `useGatewayRpc`) under jest-expo today. This is the exact pattern to mirror.
2. **The API client is unified:** web's `useApi()` returns `createApiClient(...)` from `@isol8/shared`; `ApiError` is the *same class object* on both platforms (`packages/shared/src/api/error.ts:14`). No error-class reconciliation needed.
3. **`useChatGPTOAuth` is already logic-only** (`apps/frontend/src/hooks/useChatGPTOAuth.ts`): returns `{ state, start, disconnect }`, no JSX/DOM/`window`. Only web coupling: line 1 `"use client"` and line 4 `import { useApi } from "@/lib/api"`. Uses only `api.get` + `api.post`.
4. **ChatGPT OAuth is device-code, no callback:** `POST /oauth/chatgpt/start` → `{user_code, verification_uri, expires_in, interval}`; `POST /oauth/chatgpt/poll` → `{status:"pending"}` then `{status:"completed", account_id}`. The only user-facing URL is OpenAI's own `auth.openai.com/codex/device`. Completion detected by poll — **no deep link, no `isol8://` scheme needed.**
5. **`onboarded` is already on the wire** (`container.py:444`: `"onboarded": container.get("onboarded_at") is not None`), per-container/org-shared. Mobile's `containerStatus.ts` declares `StatusBody` (lines 55-59) with only `status/substatus/phase` — it *discards* `onboarded`.
6. **Mobile funnel gate diverges:** `apps/mobile/src/components/onboarding/FirstRunGate.tsx:74` probes `GET /billing/account`; line 86 `setTrialLocal(status === "trial_local")`; line 102 renders the funnel only for `trialLocal === true`. Web gates on `container.onboarded` (`ProvisioningStepper.tsx:668`). **This is why org owners never see the mobile funnel.**
7. **Mobile runs two independent pollers:** `app/(app)/_layout.tsx:36-40` mounts `ProvisionGate` (polls `/container/status`) → `FirstRunGate` (polls `/billing/account`) → `Tabs`. Slice 2 folds these into one.
8. **First-run latch is cross-platform already:** Clerk `user.unsafeMetadata.firstRun` (`frontend/src/lib/firstRun.ts` ≈ `mobile/src/lib/firstRun.ts`). No storage seam. The only web `localStorage` is analytics-dedup (`ProvisioningStepper.tsx:496/502/545/551`) — **must stay web-side, never enter shared.**

---

## File structure

**Created:**
- `packages/shared/src/hooks/useChatGPTOAuth.ts` — the extracted device-code hook (client injected).
- `packages/shared/src/hooks/containerStatus.ts` — framework-neutral status classifier (status-code → result + blocked-payload parse). React binding stays per-platform.
- `apps/mobile/src/components/provision/ChatGPTConnectScreen.tsx` — native device-code UI (show `user_code`, open browser, poll).
- `apps/mobile/src/components/provision/ByoKeyScreen.tsx` — native BYO-key entry.

**Modified:**
- `apps/frontend/src/hooks/useChatGPTOAuth.ts` — becomes a 2-line shim `() => useChatGPTOAuthShared(useApi())`.
- `apps/mobile/src/lib/containerStatus.ts` — consume the shared classifier; surface `onboarded`.
- `apps/frontend/src/hooks/useProvisioningState.ts` — consume the shared classifier (web binding).
- `apps/mobile/src/components/provision/ProvisionGate.tsx` — host the funnel decision (Slice 2) + route `oauth_required`/`byo_provider_required` to the new native screens instead of the web punt.
- `apps/mobile/src/components/onboarding/FirstRunGate.tsx` — gate on `onboarded` (from status), drop the `/billing/account` probe.
- `packages/shared/src/index.ts` — export the new hooks/types.

**Explicitly NOT touched:** `apps/backend/**`; web's `ProvisioningStepper.tsx` phase/render logic; any Stripe/checkout/ProviderPicker/CreditsStep path.

---

## Slice sequencing

| Slice | Deliverable | Risk | Depends on |
|-------|-------------|------|------------|
| **1** | Shared container-status classifier; web + mobile both consume it (no behavior change) | Medium (shape reconciliation) | — |
| **2** | Mobile funnel reaches all owners via `onboarded`; two pollers → one | Medium (poller fold) | Slice 1 |
| **4** | In-app ChatGPT OAuth + BYO key on mobile; shared OAuth hook | Medium (net-new UI) | Slice 1 (client pattern), independent of 2 |
| **3** | *(Optional, test-gated)* shared `derivePhase` → mobile gains 4-step cold-start | High — **do not start without 3a's tests** | Slice 1 |

Slices 1→2 and 1→4 are the core. Slice 3 is optional richness and is quarantined behind a test prerequisite. Purchases are out of scope (external handoff, unchanged).

---

## Slice 1 — Shared container-status classifier

**Rationale:** Web (`useProvisioningState.ts`) and mobile (`containerStatus.ts`) are two hand-written classifiers of the same protocol — one SWR-based and well-tested, one `useState`+`AppState` and thinly tested. They *will* drift (they already have: the funnel gate). Extract the pure status-code→result + blocked-payload parse into shared; keep the React binding (SWR vs `useState`) per-platform.

**Files:**
- Create: `packages/shared/src/hooks/containerStatus.ts`
- Create: `packages/shared/src/hooks/containerStatus.test.ts`
- Modify: `packages/shared/src/index.ts`
- Modify: `apps/frontend/src/hooks/useProvisioningState.ts`
- Modify: `apps/mobile/src/lib/containerStatus.ts`

**Interfaces:**
- Produces (superset type that preserves BOTH clients' needs — mobile's pre-classified kinds AND the raw body web's stepper reads):
  ```ts
  export interface ContainerStatusBody {
    service_name: string | null; status: string | null; substatus: string | null;
    phase?: "provisioning" | "starting" | "ready" | "parked";
    onboarded?: boolean; parked?: boolean; last_error?: string | null;
    [k: string]: unknown; // tolerate the two 200 shapes (normal + synthetic)
  }
  export interface BlockedPayload {
    code: string; title: string; message: string;
    action: { kind: string; label: string; href: string; admin_only: boolean };
    owner_role: "admin" | "member";
  }
  export type ClassifiedStatus =
    | { kind: "container"; body: ContainerStatusBody }   // 200 — raw body passed through
    | { kind: "team-pending"; detail?: string }          // 202
    | { kind: "needs-signup" }                            // 404
    | { kind: "blocked"; blocked: BlockedPayload }        // 402
    | { kind: "unrecognized"; status: number };          // caller decides (web throws, mobile → unreachable)
  export function classifyContainerStatus(res: { status: number; body: unknown }): ClassifiedStatus;
  export function parseBlockedPayload(body: unknown): BlockedPayload | null; // detail.blocked w/ top-level fallback
  ```
- Consumes: `fetchJsonWithStatus` from `@isol8/shared` (already used by both).

**Landmines (from the 12-hazard audit — the classifier MUST NOT collapse these):** pass the **raw body** through on 200 (web reads `status`/`substatus`/`onboarded`/`last_error` downstream); `phase="parked"` stays in the body untouched (web treats it ready-equiv, mobile keeps polling — that's each client's binding job, not the classifier's); do NOT throw on unrecognized (return `kind:"unrecognized"` and let each caller keep its current policy — web throws, mobile → `unreachable`); keep `blocked.code` and `owner_role` intact (web branches on them). The classifier is **pure status-code + payload-shape**, nothing about polling, rendering, or ready-ness.

- [ ] **Step 1: Write failing tests** for `classifyContainerStatus` covering 200-normal, 200-synthetic (nulls + `substatus:"ensure_user_ready"`), 202, 404, 402-with-`detail.blocked`, 402-with-top-level-`blocked` fallback, and an unrecognized 500 → `kind:"unrecognized"`. Assert the raw body is preserved on 200 (including `onboarded`).
- [ ] **Step 2: Run** `pnpm --filter @isol8/shared test containerStatus` → FAIL (module missing).
- [ ] **Step 3: Implement** `classifyContainerStatus` + `parseBlockedPayload` by lifting the *shared* logic from `useProvisioningState.ts:182-215` and `containerStatus.ts:107-128` (they are equivalent except mobile lacks the top-level `blocked` fallback — keep the fallback). Export from `index.ts`.
- [ ] **Step 4: Run** the shared test → PASS.
- [ ] **Step 5: Rewire web** — `useProvisioningState.ts` calls `classifyContainerStatus` for the status→kind mapping, keeping its SWR binding, 60→30s cadence, and its throw-on-unrecognized policy. Run `apps/frontend` `useProvisioningState.test.tsx` → PASS unchanged.
- [ ] **Step 6: Rewire mobile** — `containerStatus.ts` calls `classifyContainerStatus`, keeping its `useState`+`AppState` binding, flat 5s poll, and mapping `kind:"unrecognized"` → `unreachable`. Add `onboarded` to the mobile `ContainerStatus` union so it's available downstream (Slice 2 needs it). Run mobile `containerStatus.test.tsx` → PASS.
- [ ] **Step 7: Commit** (`refactor(shared): extract container-status classifier; web+mobile consume it`).

**Done when:** both apps' existing status tests pass with the shared classifier underneath, and the mobile status object now carries `onboarded`.

---

## Slice 2 — `container.onboarded` funnel parity

**Rationale:** Mobile shows the funnel only to personal `trial_local` owners (a billing status standing in for an onboarding status), so **org owners never onboard on mobile.** Web gates on `container.onboarded` (per-container). Align mobile to the same signal, and fold the second poller (`/billing/account`) away — the `onboarded` field is already in the status Slice 1 now surfaces.

**Files:**
- Modify: `apps/mobile/src/components/provision/ProvisionGate.tsx`
- Modify: `apps/mobile/src/components/onboarding/FirstRunGate.tsx`
- Modify/Delete: the `/billing/account` probe in `FirstRunGate.tsx:74-93`
- Test: `apps/mobile/src/components/onboarding/__tests__/FirstRunGate.test.tsx`, `.../provision/__tests__/ProvisionGate.test.tsx`

**Interfaces:**
- Consumes: `ContainerStatus` now carrying `onboarded?: boolean` (Slice 1, Step 6).
- Produces: funnel decision made from container status, not a separate billing probe.

**Design:** Because `ProvisionGate` already owns the `/container/status` poll, move the funnel decision into (or adjacent to) it so there is ONE poller. `FirstRunGate` becomes: show `<FirstRunFunnel/>` when `status.kind === "ready" && status.onboarded === false && !firstRun.briefSubmitted`; render children otherwise. Treat unknown-onboarded (`loading`/`unreachable`) as **"don't show funnel, don't block"** (mirror web's brief `loading` behavior) — never trap the user behind a funnel on a transient error.

- [ ] **Step 1: Write failing test** — `FirstRunGate` renders `<FirstRunFunnel/>` for a ready+`onboarded:false` container regardless of `subscription_status` (i.e. an **org owner**, `subscription_status:"active"`), and renders children when `onboarded:true`. Add a test that `unreachable` renders children (no funnel, no trap).
- [ ] **Step 2: Run** mobile `FirstRunGate.test.tsx` → FAIL (still gates on `trial_local`).
- [ ] **Step 3: Implement** — delete the `/billing/account` probe + `trialLocal` state; derive the funnel decision from the container status (`onboarded`). Wire the single poller (lift status into a shared context/prop from `ProvisionGate`, or read the same hook). Keep the `briefSubmitted` latch.
- [ ] **Step 4: Run** mobile onboarding + provision tests → PASS.
- [ ] **Step 5: Manual/RNTL check** — `renderHook` is a Promise on mobile (await it; effects need `await/waitFor`, not `act`). Confirm an org-owner fixture reaches the funnel and a `trial_local` fixture still does.
- [ ] **Step 6: Commit** (`fix(mobile): gate first-run funnel on container.onboarded so org owners onboard`).

**Done when:** an org owner sees the funnel on mobile; the `/billing/account` probe and second poller are gone; transient errors never trap the funnel.

---

## Slice 4 — In-app ChatGPT OAuth + BYO key

**Rationale:** The cheapest net-new capability — the backend device-code endpoints are built, the hook is already logic-only, and mobile only *reads* status today. Extract the hook to shared (one param), build the native device-code screen, and route the `oauth_required`/`byo_provider_required` gates to in-app screens instead of the web punt. Purchases (Bedrock credits, the $50 sub) stay external.

**Files:**
- Create: `packages/shared/src/hooks/useChatGPTOAuth.ts` (moved), `packages/shared/src/hooks/useChatGPTOAuth.test.ts` (moved/rewired)
- Modify: `apps/frontend/src/hooks/useChatGPTOAuth.ts` (→ shim), `packages/shared/src/index.ts`
- Create: `apps/mobile/src/components/provision/ChatGPTConnectScreen.tsx`, `apps/mobile/src/components/provision/ByoKeyScreen.tsx`
- Modify: `apps/mobile/src/components/provision/ProvisionGate.tsx` (route the two provider gates in-app)

**Interfaces:**
- Produces (shared):
  ```ts
  // packages/shared/src/hooks/useChatGPTOAuth.ts
  export type OAuthState =
    | { status: "idle" } | { status: "starting" }
    | { status: "pending"; userCode: string; verificationUri: string }
    | { status: "connected"; accountId?: string } | { status: "error"; message: string };
  export function useChatGPTOAuth(api: Pick<ApiMethods, "get" | "post">): {
    state: OAuthState; start: () => void; disconnect: () => Promise<void>;
  };
  ```
- Consumes (web shim): `useApi()`. Consumes (mobile): `useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), [])` — the stable-client idiom (a `[getToken]` dep churns the poll interval; use the ref).

- [ ] **Step 1: Move** `useChatGPTOAuth.ts` body to `packages/shared/src/hooks/useChatGPTOAuth.ts`: drop line 1 `"use client"`; change signature to `useChatGPTOAuth(api)`; import `ApiError` from `../api/error`; remove the `useApi()` call. Export from `index.ts`.
- [ ] **Step 2: Web shim** — `apps/frontend/src/hooks/useChatGPTOAuth.ts` becomes `export function useChatGPTOAuth() { return useChatGPTOAuthShared(useApi()); }` + `export type { OAuthState } from "@isol8/shared";`. Run web `useChatGPTOAuth.test.tsx` + `ChatGPTOAuthStep.test.tsx` + `LLMPanel.test.tsx` → PASS unchanged (three call sites: `ChatGPTOAuthStep.tsx:15`, `LLMPanel.tsx:91`, `ChangeProviderModal.tsx:320`).
- [ ] **Step 3: Shared test** — port `useChatGPTOAuth.test` to inject a mock `api` (`{ get, post }`), assert start→pending(`userCode`,`verificationUri`)→poll→connected. Run `pnpm --filter @isol8/shared test` → PASS.
- [ ] **Step 4: Native connect screen** — `ChatGPTConnectScreen.tsx`: on mount `start()`; render the `userCode` prominently + a "Sign in to OpenAI" button that calls `WebBrowser.openBrowserAsync(verificationUri)` (**NOT `openAuthSessionAsync`** — device-code has no return URL, so an auth session never auto-closes). Poll runs in the hook; on `connected`, dismiss and let `ProvisionGate` re-poll. Add an RNTL test (await `renderHook`; effects via `waitFor`).
- [ ] **Step 5: BYO key screen** — `ByoKeyScreen.tsx`: a `TextInput` (secureTextEntry) → `api.put("/settings/keys/{provider}", { key })` then `api.post("/users/sync", { provider_choice, byo_provider })` (same endpoints as web `ByoKeyStep.tsx:22-30`). Validate non-empty; on success let the gate re-poll.
- [ ] **Step 6: Route the gates** — in `ProvisionGate.tsx`, when `blocked.code === "oauth_required"` render `<ChatGPTConnectScreen/>`; `=== "byo_provider_required"` render `<ByoKeyScreen/>` — instead of the "Manage on isol8.co" punt. Leave `credits_required`/`subscription_required`/`team_billing_required`/`provider_required` (Bedrock/purchase/pick) as the existing external handoff.
- [ ] **Step 7: Run** all touched web + shared + mobile tests → PASS. **Commit** (`feat(mobile): in-app ChatGPT OAuth + BYO key via shared hook`).

**Done when:** on mobile, an owner in `oauth_required` connects ChatGPT in-app (browser hop + poll, no deep link) and `byo_provider_required` enters a key in-app; web behavior is byte-for-byte unchanged; purchases still hand off to web.

---

## Slice 3 — *(OPTIONAL, TEST-GATED)* Shared `derivePhase` cold-start

**Do not start 3a without completing 3-tests.** The web cold-start phase memo (`ProvisioningStepper.tsx:393-438`) is pure data→Phase (no elapsed-time dep — the module header comment claiming otherwise is **stale**), but its `isRegressingFromReady` / parked→ready / error→container / payment branches have **zero direct test coverage.** Extracting under-tested logic is the plan's single highest-risk act.

**Files:**
- Create: `packages/shared/src/hooks/derivePhase.ts`, `packages/shared/src/hooks/derivePhase.test.ts`
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` (consume shared `derivePhase`)
- Modify: `apps/mobile/src/components/provision/ProvisionGate.tsx` (render a lite stepper off the shared phase)

- [ ] **3-tests (PREREQUISITE): Write DIRECT unit tests** against the *current* web memo behavior for EVERY branch — `regression-guard` (ready then non-ready), `parked→ready-equiv`, `error→container` (keys on `status`, not `phase`), `provisioning`+`task_running`→`gateway`, `starting`+`gatewayHealth`→`agent`, `payment`. These tests must pass against the code as-is (they pin behavior). If any branch's intended behavior is unclear, STOP and resolve before extracting.
- [ ] **3a: Extract** `derivePhase(inputs): Phase` to shared (pure function). Rewire the web memo to call it. Run the 3-tests + web stepper tests → PASS unchanged.
- [ ] **3b: Mobile lite stepper** — render container→gateway→agent progression from the shared phase in `ProvisionGate` (replacing the single "starting" banner). Only worth doing if mobile wants the granular progression; a single banner is acceptable — **descope this slice entirely under any schedule pressure.**
- [ ] **Commit** per step.

---

## Out of scope (deliberate)

- **In-app Stripe checkout / ProviderPicker / CreditsStep / subscription purchase** — stay as outbound `Linking.openURL` handoff to isol8.co (Apple 3.1.1; ~40% of web's state machine, dead weight on iOS).
- **Any backend change** — the gate, status, OAuth, and brief endpoints are already client-agnostic.
- **Mobile client-side funnel analytics** — web's PostHog captures are localStorage-deduped and stay web-side; the backend already emits `trial_started` + `first_run_brief_submitted`. Accept the gap or build separately later.

## Risks & mitigations (ranked)

1. **Apple IAP (3.1.1)** — keep purchases external (Global Constraints). Highest severity, fully avoided by scope.
2. **`derivePhase` under-tested** — Slice 3's 3-tests are a hard prerequisite; descope Slice 3 if pressured.
3. **Classifier shape unification** — design `ContainerStatusBody` as a superset that passes the raw 200 body through; keep React bindings + cadences per-platform (Slice 1 landmines list).
4. **SWR key literal `"/container/status"`** — never rename; grep the string before touching `useProvisioningState`.
5. **Two-poller fold (Slice 2)** — treat unknown-onboarded as don't-show/don't-block to avoid funnel flash/trap.

## Residual unknowns to resolve during execution

- The exact unified `ContainerStatusBody` field set (Slice 1) — finalize against both consumers' reads; err toward passing the whole body through.
- Whether mobile wants the granular cold-start (Slice 3) at all — product call; default is "no, keep the banner."
- Submit-time 3.1.1(a) external-link disclosure mechanics — confirm against current Apple rules at submission (out of code scope).
