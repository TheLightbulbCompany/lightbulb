# OAuth Step Idempotency Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use `- [ ]` syntax for tracking.

**Goal:** After a user completes ChatGPT OAuth, the user advances cleanly into the provisioning UI. The "Sign in with ChatGPT" button must not reappear during the eventual-consistency window between OAuth-row write and the next gate evaluation, and re-mounting `ChatGPTOAuthStep` (e.g., after a reload mid-OAuth) must NOT show the idle "Connect ChatGPT" CTA when tokens are already on file.

**Tech stack:** Python 3.13 (FastAPI, uv), pytest. Next.js 16 / React 19, TypeScript, SWR 2.x, vitest, @testing-library/react.

**Premise:** This plan stacks on `origin/main` and is intentionally narrower than the still-unmerged 2026-05-08-chatgpt-oauth-and-status-hook-consolidation.md. It picks two tasks from that plan (render-order swap, auto-start on mount) and adds a new `GET /oauth/chatgpt/status` endpoint so the wizard is a true read-your-writes idempotent surface even if the broader hook consolidation lands later.

---

## Background and verified diagnosis

**Reproduction:** signup → click ChatGPT card → land on /chat?provider=chatgpt_oauth → click "Connect ChatGPT" → see verification URL + code → finish at OpenAI → expect cold-start spinner → see green "Sign in with ChatGPT" button instead. Refresh recovers.

**Mechanism (verified by reading code at HEAD):**

1. `ChatGPTOAuthStep` renders only when `phase === "provider"` (`apps/frontend/src/components/chat/ProvisioningStepper.tsx:667-689`). Phase is local-derived (`apps/frontend/src/components/chat/ProvisioningStepper.tsx:356-403`); it depends on `needsProviderStep` (URL-derived) and is NOT sourced from `useProvisioningState`.
2. The `useProvisioningState`-driven gate-blocked early return (`apps/frontend/src/components/chat/ProvisioningStepper.tsx:480-627`) executes BEFORE the provider-step return. When `/container/status` returns 402 with `code: "oauth_required"`, the gate UI renders on top of any provider-step UI.
3. After `poll_device_code` writes `state="active"` to the OAuth row (`apps/backend/core/services/oauth_service.py:263-274`), the next `/container/status` call evaluates `_has_oauth_tokens` (`apps/backend/core/services/provision_gate.py:147-160`) which does an eventually-consistent DDB `get_item`. During the read-after-write window the gate returns 402 oauth_required and the green gate UI flashes.
4. Independently: if `ChatGPTOAuthStep` ever unmounts and remounts (e.g., user reloads after OAuth completes but before `?provider=` is stripped, or a transient unmount caused by parent re-render), `useChatGPTOAuth` initializes at `{status: "idle"}` (`apps/frontend/src/hooks/useChatGPTOAuth.ts:43`) — there is no check that tokens already exist server-side. The user sees the Connect button despite being already connected.

The user's diagnosis attributed the visible bug to (4); the dominant cause in practice is (2)+(3). Both are real and the fix should close both.

---

## Two viable design alternatives (and a third)

### Option A — Add `GET /oauth/chatgpt/status`; `useChatGPTOAuth` self-checks on mount

**Shape:**
- New backend route `GET /oauth/chatgpt/status` returning `{connected: bool, account_id: str | null}`.
- `useChatGPTOAuth` runs a one-shot check on mount; if `connected: true`, jumps straight to `{status: "completed", accountId}` and skips `start()`.

**Pros:**
- Hook becomes idempotent. Any future remount sequence — reload, parent re-render, RSC navigation — recovers correctly.
- Read-your-writes for the OAuth-just-finished case if the new endpoint uses **strongly consistent** DDB reads.
- Small surface area; all the new logic is inside the hook + one route.
- Composable with the larger 2026-05-08 plan; both can land in either order.

**Cons:**
- Adds an extra HTTP call on every mount of `ChatGPTOAuthStep` (and on every mount of `LLMPanel` if the hook gets reused — currently only `disconnect` is destructured there, so no fetch races today).
- Doesn't fix the gate-blocked early-return racing the provider step when the user is mid-OAuth — that requires the render-order swap from the 2026-05-08 plan as well.

### Option B — Lift OAuth-completion state into the parent (or `useProvisioningState`)

**Shape:**
- Either: `ProvisioningStepper` passes a `chatgptConnected` boolean to `ChatGPTOAuthStep` and refuses to mount the idle branch when true.
- Or: `useProvisioningState` (or its backend `/container/status` payload) gains a new `oauth_connected` field; the stepper consults it and never mounts `ChatGPTOAuthStep` after completion.

**Pros:**
- Eliminates the remount race at its source (no remount happens).
- One fewer HTTP endpoint surface than Option A.

**Cons:**
- Requires changes to `ProvisioningStateResult` (`apps/frontend/src/hooks/useProvisioningState.ts:32-38`) and the backend `/container/status` shape (`apps/backend/routers/container.py`).
- `LLMPanel` and any future surface that uses `useChatGPTOAuth` outside the stepper still has no way to see "tokens already exist" — the hook itself stays non-idempotent.
- Cross-cuts the 2026-05-08 plan more aggressively (would have to be redesigned if that lands first).
- Doesn't reduce the dual-hook cache war; if anything, it worsens coupling between the gate state and OAuth state.

### Option C — Render-order swap + auto-start (no new endpoint, no parent state)

**Shape:** Cherry-pick Tasks 6 + 7 from `docs/superpowers/plans/2026-05-08-chatgpt-oauth-and-status-hook-consolidation.md`:
- Task 6: Move the `gatePhase === "blocked"` early return AFTER the `phase === "provider"` early return so the provider step wins when `?provider=` is in the URL.
- Task 7: Auto-start the device-code request on mount of `ChatGPTOAuthStep` so the idle "Connect ChatGPT" button branch is gone — replaced by a "Starting ChatGPT sign-in..." spinner.

**Pros:**
- No new endpoint. No new backend code.
- Eliminates the dominant visible failure mode (gate UI re-rendering on top of OAuth).
- The auto-start removes the idle button entirely; even if a remount happens, the user sees a spinner-then-device-code, not a Connect button.

**Cons:**
- Auto-start fires `POST /oauth/chatgpt/start` automatically. If a `pending` row already exists for this user, that's harmless (the put_item ConditionExpression at `apps/backend/core/services/oauth_service.py:134-137` only blocks `state="active"`; pending rows get overwritten). But if an `active` row already exists (user already completed OAuth, came back to /chat?provider=chatgpt_oauth), it 409s and the hook lands in `state: "error"` — user sees an error message instead of "you're already connected."
- Doesn't make `useChatGPTOAuth` idempotent for surfaces other than the wizard. (Today there are none, but adding one would re-introduce the bug.)

---

## Recommendation

**Implement Option A AND Option C together.** They are complementary, not alternatives:

- Option C fixes the dominant visible failure (gate UI flashing on top of the wizard) and strips the "second click required" friction.
- Option A makes the hook idempotent in the case where `start()` would otherwise 409 because tokens are already active — covering the auto-start's failure mode in C.

This keeps the plan scope small and aligned with the larger 2026-05-08 plan: if that plan lands first, this one collapses to "just add the status endpoint + hook self-check"; if this plan lands first, it forward-compatibly covers two of the 2026-05-08 plan's tasks.

Reject Option B alone: it doesn't fix the surface area outside the stepper (LLMPanel + future), and it bloats `ProvisioningStateResult`'s contract for a problem the hook itself should own.

---

## File structure

### Files to **modify**

| Path | Why |
|---|---|
| `apps/backend/routers/oauth.py` | Add `GET /status` route. |
| `apps/backend/tests/unit/routers/test_oauth.py` | Pin /status response shape + 401/403 behavior. |
| `apps/frontend/src/hooks/useChatGPTOAuth.ts` | Call /status on mount; if connected, set `status: "completed"`; else proceed. Auto-start hook (Option C, integrated). |
| `apps/frontend/src/components/chat/ChatGPTOAuthStep.tsx` | Drop the idle "Connect ChatGPT" button branch. Render a spinner while the hook resolves status / starts device code. |
| `apps/frontend/src/components/chat/ProvisioningStepper.tsx` | Move the `gatePhase === "blocked"` early return AFTER the `phase === "provider"` early return. |
| `apps/frontend/src/components/chat/__tests__/ProvisioningStepper.test.tsx` | New test: gate fires AND `?provider=chatgpt_oauth` → wizard renders, gate UI does not. |

### Files to **create**

| Path | Why |
|---|---|
| `apps/frontend/src/hooks/__tests__/useChatGPTOAuth.test.tsx` | No tests today. Pin status-on-mount behavior + auto-start guard. |
| `apps/frontend/src/components/chat/__tests__/ChatGPTOAuthStep.test.tsx` | Pin auto-start, completed-on-mount-via-status, pending render. |

### Files to **NOT** touch

- `apps/backend/core/services/oauth_service.py` — `get_decrypted_tokens` already exists at line 303 and returns `dict | None` (None when no row OR row not active). It's exactly the predicate we need; the only nit is that DDB reads are eventually consistent by default. We pass `ConsistentRead=True` from the new route's call path (see Task 1 step 3) without modifying the helper itself, to avoid hardening the entire helper for a single new caller.
- `apps/backend/core/services/provision_gate.py` — out of scope. The 402 race window narrows but isn't fully closed by this plan; closing it requires the render-order swap (Task 3) so the provider step wins anyway.
- `useProvisioningState.ts` — out of scope. The 2026-05-08 plan owns hook consolidation.

---

## Tasks

### Task 1 — Backend: `GET /oauth/chatgpt/status` endpoint

**Files:**
- Modify: `apps/backend/routers/oauth.py`
- Modify: `apps/backend/tests/unit/routers/test_oauth.py`

**Endpoint contract:**
- Method: `GET`
- Path: `/oauth/chatgpt/status` (full path with API prefix: `/api/v1/oauth/chatgpt/status`)
- Auth: `Depends(get_current_user)` — same as `/start`, `/poll`, `/disconnect`. 401 propagates normally.
- Org-context guard: **YES, mirror `/start`'s 403.** ChatGPT OAuth is personal-only per `memory/project_chatgpt_oauth_personal_only.md`. Even though "asking whether you have tokens" is read-only, an org-context user has no legitimate use for this endpoint and we keep the guard for symmetry. (Belt-and-suspenders: a future "reconnect" UI in an org would have the same status of "not allowed.")
- Response: `{connected: bool, account_id: str | null}` (200 always when authenticated; never raises for "no row").
- Implementation: call `oauth_service.get_decrypted_tokens(user_id=ctx.user_id)` then return `connected=tokens is not None`, `account_id=tokens.get("account_id") if tokens else None`. We do NOT decrypt and return tokens themselves. The decryption cost on a hot read path is acceptable today (DDB single-key get + Fernet decrypt < 5ms in practice); revisit if status becomes a chatty endpoint.
- DDB consistency: pass `ConsistentRead=True` from the new endpoint to mitigate the read-after-write window. We do this at the route level (a thin variant call against `_table().get_item(..., ConsistentRead=True)`) rather than modifying `get_decrypted_tokens` so we don't tax other callers (gate evaluator, provisioning) with strong reads.

**Steps:**

- [ ] **Step 1.** Write a failing test in `apps/backend/tests/unit/routers/test_oauth.py`. Add a test class `TestStatus` mirroring the pattern in `TestStartProviderRules`:
  - `test_status_returns_connected_true_when_active_row_exists` — patch `oauth_service.get_decrypted_tokens` (or whatever the route calls) to return `{"account_id": "acc_x", "access_token": "...", "refresh_token": "..."}`; assert 200, body `{"connected": true, "account_id": "acc_x"}`.
  - `test_status_returns_connected_false_when_no_row` — patch the lookup to return `None`; assert 200, `{"connected": false, "account_id": null}`.
  - `test_status_returns_connected_false_when_row_inactive` — same as above (the helper already returns None when state != "active").
  - `test_status_rejects_org_context` — use the `override_org_admin_auth` fixture; assert 403, helper not awaited.
  - `test_status_passes_consistent_read` — assert that whatever DDB call the route uses received `ConsistentRead=True` (use `mock_table.get_item.assert_called_with(..., ConsistentRead=True)`).
- [ ] **Step 2.** Run: `cd apps/backend && uv run pytest tests/unit/routers/test_oauth.py::TestStatus -v`. Expected: failures.
- [ ] **Step 3.** Implement the route in `apps/backend/routers/oauth.py` after the `disconnect` route. Skeleton:
  ```python
  @router.get(
      "/status",
      summary="Whether the user has stored ChatGPT OAuth tokens",
      description=(
          "Read-only probe used by the wizard's hook on mount to recover "
          "from a remount mid-flow. Returns 200 with {connected, account_id}; "
          "never raises for missing-row. 403 for org-context users (ChatGPT "
          "OAuth is personal-only — see "
          "memory/project_chatgpt_oauth_personal_only.md)."
      ),
  )
  async def status(ctx: AuthContext = Depends(get_current_user)):
      if ctx.is_org_context:
          raise HTTPException(
              status_code=403,
              detail="ChatGPT OAuth is not available for organization workspaces.",
          )
      # Strongly-consistent read so the wizard sees its own write from /poll
      # without an eventual-consistency stutter.
      from core.services import oauth_service
      tokens = await oauth_service.get_decrypted_tokens_consistent(user_id=ctx.user_id)
      return {
          "connected": tokens is not None,
          "account_id": (tokens or {}).get("account_id"),
      }
  ```
  Add a small wrapper in `oauth_service.py` named `get_decrypted_tokens_consistent` that does `_table().get_item(Key=..., ConsistentRead=True).get("Item")` and otherwise mirrors `get_decrypted_tokens`. Keeping it as a separate helper — rather than a parameter on the existing function — preserves the existing call sites' read characteristics.
- [ ] **Step 4.** Re-run the tests; iterate until green.
- [ ] **Step 5.** Commit. Suggested message:
  ```
  feat(oauth): add GET /oauth/chatgpt/status for wizard idempotency

  Returns {connected, account_id} so the frontend hook can recover from
  a remount mid-flow without showing the Connect button to a user whose
  tokens are already on file. Strongly-consistent DDB read so the
  wizard sees its own write from /poll without a stutter. 403 for
  org-context users (personal-only path; see
  memory/project_chatgpt_oauth_personal_only.md).
  ```

### Task 2 — Frontend: `useChatGPTOAuth` checks status on mount + auto-start

**Files:**
- Modify: `apps/frontend/src/hooks/useChatGPTOAuth.ts`
- Create: `apps/frontend/src/hooks/__tests__/useChatGPTOAuth.test.tsx`

**Behavior:**
- On mount, fire `GET /oauth/chatgpt/status` exactly once.
- If `connected: true` → set state to `{status: "completed", accountId}`. Do not call `start()`.
- If `connected: false` → call `start()`. (This is the Option C auto-start; combined here so we don't duplicate the on-mount effect.)
- If the status call 401s — let it bubble; api layer redirects to sign-in. If it 403s (shouldn't happen for a personal user, but defensive) — fall back to calling `start()` (the existing behavior). If it network-fails — surface as `state: "error"` with a "Try again" path.
- Guard against React 19 StrictMode double-invoke with a `useRef` (see existing pattern in the 2026-05-08 plan Task 7).

**Steps:**

- [ ] **Step 1.** Write failing tests in `apps/frontend/src/hooks/__tests__/useChatGPTOAuth.test.tsx`:
  - `mounts_completed_when_status_returns_connected` — mock `fetch` to return 200 `{connected: true, account_id: "acc_x"}` for `/oauth/chatgpt/status`; render the hook; expect `state.status === "completed"` and start NOT called.
  - `mounts_pending_when_status_returns_disconnected` — mock status → `{connected: false}`, then mock `/start` → device-code response; expect `state.status === "pending"` with the right userCode.
  - `does_not_double_fire_under_strictmode` — render the hook in `<StrictMode>`; assert `/status` fetched exactly once and `/start` fetched at most once.
  - `surfaces_status_failure_as_error_state` — mock `/status` → 500; expect `state.status === "error"`. (This is intentionally conservative; we'd rather show an error than auto-start a possibly-redundant device-code flow.)
- [ ] **Step 2.** Run: `cd apps/frontend && pnpm test -- --run src/hooks/__tests__/useChatGPTOAuth.test.tsx`. Expected: failures.
- [ ] **Step 3.** Edit `apps/frontend/src/hooks/useChatGPTOAuth.ts`:
  - Add a new `useEffect` (with a `startedRef` guard) that runs once: GET /oauth/chatgpt/status; branch into the right setState.
  - Remove the existing call to `start()` from `ChatGPTOAuthStep` (Task 3) — auto-start lives here now.
- [ ] **Step 4.** Iterate until green. Run the broader hooks suite to confirm no regressions: `cd apps/frontend && pnpm test -- --run src/hooks/`.
- [ ] **Step 5.** Commit. Suggested message:
  ```
  feat(useChatGPTOAuth): on-mount status probe + auto-start; hook is now idempotent

  The hook calls GET /oauth/chatgpt/status once on mount. If tokens are
  on file, it lands in {status: completed} and never fires /start.
  Otherwise it auto-starts the device-code flow. StrictMode-safe via
  startedRef. Replaces the previous user-clicks-Connect entry point.
  ```

### Task 3 — Frontend: `ChatGPTOAuthStep` drops the idle button branch

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatGPTOAuthStep.tsx`
- Create: `apps/frontend/src/components/chat/__tests__/ChatGPTOAuthStep.test.tsx`

This is the same edit as Task 7 from the 2026-05-08 plan, copied here for completeness. Replace the idle branch with a "Starting ChatGPT sign-in..." spinner. Restyle the error-retry button to green (`#06402B`) for consistency with the gate UI's color (which Task 4 may make moot when the gate UI is no longer being raced).

**Steps:**

- [ ] **Step 1.** Write failing tests in `__tests__/ChatGPTOAuthStep.test.tsx` covering:
  - `does_not_render_connect_button_in_idle` — mock the hook to return `state.status: "idle"`; assert the Connect button is not in the DOM (a spinner is).
  - `renders_device_code_in_pending` — same shape as the 2026-05-08 plan's Task 7 test.
  - `calls_onComplete_in_completed` — same shape as the 2026-05-08 plan's Task 7 test.
- [ ] **Step 2.** Run; expected: failures.
- [ ] **Step 3.** Replace the body of `ChatGPTOAuthStep` per the 2026-05-08 plan's Task 7 step 3 (the file content is fully spelled out there). One delta from that plan: do NOT call `start()` from the component — Task 2's hook already auto-starts.
- [ ] **Step 4.** Iterate until green.
- [ ] **Step 5.** Commit. Suggested message:
  ```
  feat(ChatGPTOAuthStep): drop idle Connect button; rely on hook auto-start

  The hook now self-starts on mount, so the idle button is dead UI. A
  remount that finds tokens already on file (via the new /status probe)
  lands directly in {status: completed} without ever showing this branch.
  ```

### Task 4 — Render-order swap so provider step beats gate-blocked

**Files:**
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx`
- Modify: `apps/frontend/src/components/chat/__tests__/ProvisioningStepper.test.tsx`

This is identical to Task 6 of the 2026-05-08 plan. The full diff and test text are spelled out there; reproduce them here for self-containment.

**Steps:**

- [ ] **Step 1.** Add the failing test from the 2026-05-08 plan Task 6 step 1.
- [ ] **Step 2.** Run; expected: failure (gate UI text still rendered).
- [ ] **Step 3.** Move the `if (gatePhase === "blocked" && blocked)` block (currently lines 480-627) to AFTER the `if (phase === "provider" && providerChoice !== null)` block (currently lines 667-689).
- [ ] **Step 4.** Run; expected: green for the new test AND the existing gate-blocked test (the latter has no `?provider=` param, so it still falls through to the gate UI).
- [ ] **Step 5.** Commit. Suggested message:
  ```
  fix(ProvisioningStepper): provider step wins over gate-blocked early return

  When the URL has ?provider=chatgpt_oauth the user is mid-OAuth-completion;
  rendering the gate UI on top of the wizard would re-show the green
  "Sign in with ChatGPT" button DURING the eventual-consistency window
  between the OAuth row write and the next /container/status poll.
  Reorder so the provider step takes priority when its URL flag is set.
  ```

### Task 5 — Verification + PR

- [ ] **Step 1.** Run full backend tests: `cd apps/backend && uv run pytest -q`.
- [ ] **Step 2.** Run full frontend tests: `cd apps/frontend && pnpm test -- --run`.
- [ ] **Step 3.** Lint: `cd apps/frontend && pnpm run lint && cd ../backend && uv run ruff check . && uv run ruff format --check .`.
- [ ] **Step 4.** Manual on dev: fresh personal signup → click ChatGPT card → wizard auto-fires device code (no "Connect" button) → finish at OpenAI → cold-start spinner appears immediately, no green button flash.
- [ ] **Step 5.** Manual edge case: while wizard is showing the device code, reload the page. Wizard re-mounts; `/status` is hit; if tokens were just written, it returns connected:true and the user advances to provisioning. If tokens not yet written, it auto-starts a fresh device code (the prior pending row is overwritten harmlessly per oauth_service:134-137).
- [ ] **Step 6.** Open the PR.

---

## Risk assessment

**What this fix could break:**

1. **`useChatGPTOAuth` is also imported by `LLMPanel`** (`apps/frontend/src/components/control/panels/LLMPanel.tsx:7,56`) but only `disconnect` is destructured. The new on-mount `/status` fetch will fire whenever LLMPanel mounts. Mitigation: `LLMPanel` only renders inside the post-onboarding control panel, so a one-shot read on entry is acceptable and gives the panel a free signal to display "Connected as <account>" — the existing test (`LLMPanel.test.tsx:22-23`) mocks the hook entirely and won't break. **Action:** verify the panel doesn't crash by checking the test passes under the new return shape; no source change required because the test mocks the hook surface.

2. **Race between on-mount `/status` and `useProvisioningState`'s polling.** Could the wizard's status call land just BEFORE the gate evaluator reads, causing inconsistency? The wizard's `/status` is read-only and doesn't mutate anything; it cannot affect what `/container/status` returns. The only inconsistency would be a one-frame stutter where the wizard says "completed" but the gate UI thinks oauth_required — which Task 4's render-order swap fixes by making the gate UI not render at all when `?provider=` is set. So the two interact correctly when both tasks land.

3. **Auto-start fires `/oauth/chatgpt/start` for users who already have an `active` row but happen to land on `?provider=chatgpt_oauth` again.** Without the `/status` probe (Option C alone), this would 409 → `state: "error"`. With the probe (Option A combined), the hook short-circuits to `{status: "completed"}` before calling `start()`. So Option A genuinely closes Option C's failure mode.

4. **DDB ConsistentRead doubles read-unit cost for the new endpoint.** Negligible: this endpoint is hit once per wizard mount per user. Order-of-magnitude single-digit RUs/day per user.

5. **Org-only guard on the new endpoint.** Mirrors `/start`. The memory file `memory/project_chatgpt_oauth_personal_only.md` says this is personal-only; an org-context user calling `/status` should get the same 403 they get from `/start`. The reverse case (personal user calling `/status` from a future org context) is impossible because the JWT carries the active org.

6. **What if the on-mount `/status` call is slow (network jitter, cold lambda)?** The wizard renders the spinner state from Task 3 in the meantime — same UX as today's "starting" stretch. Worst case is the same as today (a few seconds of spinner before the device code).

7. **Does the on-mount `/status` call double-fire under React 19 StrictMode?** Guarded by the `startedRef` in Task 2, with an explicit test (Task 2 step 1).

---

## Self-review

**Coverage:**
- "Sign in with ChatGPT button reappears" → Task 4 (render-order swap) eliminates the gate UI from rendering on top of the wizard when `?provider=` is present. ✓
- "Page refresh recovers" → Task 2's `/status` probe makes refresh deterministic regardless of timing. ✓
- "useChatGPTOAuth has NO check for tokens-already-stored" → Task 1 adds the endpoint, Task 2 wires the hook. ✓
- "/start 409 if user clicks again" → Task 3 deletes the button entirely; Task 2's status probe prevents auto-start when already connected. ✓
- "Org-only guard" → Task 1 step 3 adds the same 403 as `/start`. ✓

**Out of scope (deferred to 2026-05-08 plan):**
- The dual-hook SWR cache war between `useContainerStatus` and `useProvisioningState`. Worsens the symptom but isn't the root cause of "button reappears."
- Hook consolidation (`useContainerStatus` deletion).
- The phantom `/settings/llm` route in the gate's `action_href`.

If 2026-05-08 lands first, Tasks 3 and 4 of this plan become no-ops; Tasks 1 and 2 still apply on top.

---

## File-path checklist (for reviewers)

- Backend route: `apps/backend/routers/oauth.py`
- Backend service helper: `apps/backend/core/services/oauth_service.py`
- Backend route test: `apps/backend/tests/unit/routers/test_oauth.py`
- Frontend hook: `apps/frontend/src/hooks/useChatGPTOAuth.ts`
- Frontend hook test (new): `apps/frontend/src/hooks/__tests__/useChatGPTOAuth.test.tsx`
- Frontend component: `apps/frontend/src/components/chat/ChatGPTOAuthStep.tsx`
- Frontend component test (new): `apps/frontend/src/components/chat/__tests__/ChatGPTOAuthStep.test.tsx`
- Frontend stepper: `apps/frontend/src/components/chat/ProvisioningStepper.tsx`
- Frontend stepper test: `apps/frontend/src/components/chat/__tests__/ProvisioningStepper.test.tsx`
- Memory: `memory/project_chatgpt_oauth_personal_only.md` (consulted, not modified)
- Reference plan: `docs/superpowers/plans/2026-05-08-chatgpt-oauth-and-status-hook-consolidation.md`
