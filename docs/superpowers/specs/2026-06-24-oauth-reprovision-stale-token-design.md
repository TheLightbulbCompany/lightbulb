# Design: ChatGPT OAuth — reprovisions & updates must not resurrect a stale token

**Date:** 2026-06-24
**Status:** Approved (design); pending implementation plan
**Owner:** backend (container auth / oauth)

## Problem

A ChatGPT-OAuth owner's container went dead with a `refresh_token_reused` (HTTP 401)
auth error — *not* a usage/quota error. Verified live on 2026-06-23 against the
founders' org container `org_3DBS8G83oPrMm49yN2b70EjoFpN` (image
`2026.6.6-isol8.2`, `bedrock_revoked=true` so there is no fallback provider):

- A reprovision booted the container at ~06:02 UTC (six fresh
  `paperclip-agent-keys` secrets created at 06:00; task start 06:02).
- The **first** OAuth refresh after that clean boot — at 06:08:52 UTC — already
  returned `refresh_token_reused`. A token that 401s as "already used" on the
  *first* attempt after boot means the container booted with an **already-spent
  refresh token**: it imported a dead credential.

### Root cause: DDB↔container store-of-record divergence

OpenAI rotates the OAuth refresh token on **every** use. That rotation happens
**inside the container**, in its per-agent SQLite auth store
(`agents/main/agent/openclaw-agent.sqlite`), on OpenClaw's schedule. The Lightbulb AI
backend is not in that loop, so the rotated token is never written back to DDB.

The DDB `oauth-tokens` row is therefore a **frozen snapshot** from the last
connect (here: a reconnect on 06-19). It goes stale the instant the container
performs its first refresh.

The reprovision re-seeded the container from that stale DDB row. The mechanism is
a single destructive call: `ecs_manager._pre_stage_oauth_for_user` does **stage +
clear** —

1. `workspace.pre_stage_auth_profile_store(...)` writes the DDB token to
   `<EFS>/users/{owner}/credentials/oauth.json`.
2. `workspace.clear_default_agent_auth_profile_row(...)` (PR #935) deletes the
   container's SQLite `main` auth row so the boot **re-import** fires.

On a fresh provision step 2 is a harmless no-op (no row yet). But on a
**reprovision/update of an existing container**, step 2 wipes the container's
*live, rotated* token and forces re-import of the **stale** EFS seed → spent
token → `refresh_token_reused` on first refresh → with Bedrock revoked, the agent
is fully dead.

### Why prior fixes don't cover this

| Fix | Fixed | Covers this? |
|-----|-------|--------------|
| PR #873 | per-agent **fan-out** inside one container | No — this isn't fan-out |
| PR #935 | the import **not firing** on switch/reconnect | No — it fired, just re-imported a *stale* token; #935 made the re-seed reliably destructive on ops paths |
| 06-19 manual recovery | one-time SQLite row delete after a *then-fresh* reconnect | One-off; didn't change the staleness mechanism |

## Principle (the data model, stated honestly)

> **The container's SQLite auth store is the source of truth for the live ChatGPT
> OAuth token. DDB `oauth-tokens` is a *bootstrap seed* — authoritative only for a
> container that has no credential yet, and never re-trusted after the container
> owns a live credential.**

DDB should **not** be the source of truth: the container is the only component
present at each refresh-token rotation, so DDB can only ever be a lagging copy.
The legitimate purpose of the DDB token is a one-time **handoff buffer** — the
user authenticates in the web UI, the container is created *afterward*, so the
token is parked in DDB to stage into that new container on first boot. The bug was
treating that bootstrap seed as a live mirror and re-trusting it on every ops
event.

The `clear` (force-reimport) is the only safe-to-overwrite-the-container action,
and it is only safe when the token being staged is genuinely fresh/unspent.
Genuine fresh-token events are exactly two: **reconnect** and **provider switch**.

## The fix

### 1. Ops paths stop clobbering — remove the `clear` from the shared helper

`core/containers/ecs_manager.py:_pre_stage_oauth_for_user` currently calls
`clear_default_agent_auth_profile_row` (the destructive step). **Remove that
call** from this helper. Both ops callers go through it and become non-clobbering
at once:

- `ecs_manager:1518` — provision/recover pre-task
- `ecs_manager:2269` — `write_user_configs` (hit by reprovision / config-rewrite /
  scale-from-zero / debug PATCH / reapply)

Staging (`pre_stage_auth_profile_store`) **stays**: it is idempotent, and the
container's import gate is **store-row-absent**, so writing `oauth.json` over a
populated store is inert — the container keeps its live token. A genuinely fresh
container (empty store) still imports the staged seed on first boot with no
`clear` needed.

### 2. Reconnect must reach the running container — add stage + clear to `/poll`

A reconnect of the *same* provider does **not** trigger a reprovision and does
**not** hit `_hot_swap_provider` (which only fires on a `provider_choice`
*change*, `billing_service:731`). Today `routers/oauth.py:/poll` (the
connect-complete handler) writes the fresh token to DDB but never pushes it to the
running container — the C2 gap. So after `poll_device_code` completes and the
connector is claimed, **push the fresh credential to the container**: stage the
new `oauth.json` + `clear` the `main` row so the unspent token re-imports on the
next run.

This is safe and correct because the token is brand-new. Best-effort: if the
container is not provisioned yet (first-ever connect during onboarding), the EFS
path / SQLite may not exist — the helper no-ops and the provision pre-task
(step 1's staging) seeds it at create time.

### 3. Keep stage + clear in `_hot_swap_provider`

`billing_service._hot_swap_provider` (switch *into* `chatgpt_oauth`) already does
stage + clear with a freshly re-read token (`billing_service:839/849`). It is the
other genuine fresh-token event. Leave it as-is.

### 4. Factor the "push fresh credential to container" helper (orthogonality)

Steps 2 and 3 do the same two operations (stage + clear, best-effort). Factor a
single helper — e.g. `push_fresh_oauth_credential(owner_id, connector_id)` — so
reconnect and switch share one implementation, and `_pre_stage_oauth_for_user`
remains stage-only for ops paths. (Exact home/signature decided in the plan;
`_hot_swap_provider` can adopt it or keep its inline calls — the plan will pick
the lower-churn option.)

### 5. Cheap hygiene — gate checks `state=active` without decrypting

`provision_gate._has_oauth_tokens` currently calls `oauth_service.get_decrypted_tokens`
purely to answer "is ChatGPT connected?" — decrypting the blob to check existence.
Add an existence/`state == "active"` check (e.g. `oauth_service.has_active_oauth`)
that does **not** decrypt, and use it in the gate. This decouples the
connected-flag from the secret at the read site — the clean, zero-risk half of
"don't lean on the stored secret."

## Invariants after the fix

- `clear_default_agent_auth_profile_row` (force-reimport) fires **only** on the
  two genuine fresh-token events: reconnect (`/poll`) and switch
  (`_hot_swap_provider`). Never on reprovision / recover / update / config-rewrite.
- Staging an `oauth.json` over a populated container store is inert (import gate is
  store-row-absent). A live, rotating in-container token is never destroyed by an
  ops event.
- Fresh provision: empty store → imports the staged seed on first boot.
- Reprovision/recover/update: store preserved → container keeps its live token; the
  re-staged seed is ignored.

## Edge cases / error handling

- **Disaster recovery (container store wiped — corruption repair, AP recreate):**
  the container boots unauthenticated and the user must reconnect. This is no worse
  than today — a *spent* stale seed could never have recovered a wiped store
  anyway. Documented, not engineered around.
- **Switch *away* from `chatgpt_oauth`:** already revokes (`revoke_user_oauth` +
  `delete_auth_profile_store` + `models.authLogout`). Switching back already
  requires a reconnect today; unchanged.
- **Concurrent reconnect + reprovision:** both converge on the same fresh DDB
  token; staging is idempotent; safe.
- **`/poll` for an org connector:** stage under the **container owner**
  (`bound_owner_id`), read tokens under the **connector** (`ctx.user_id`) — mirror
  the existing `user_id` vs `token_owner_id` split in `_pre_stage_oauth_for_user`
  and `_hot_swap_provider`.

## Testing strategy (mockable — no live container required)

- `_pre_stage_oauth_for_user` **does not** call
  `clear_default_agent_auth_profile_row` (assert the destructive call is gone from
  the ops path).
- `/poll` completion pushes the fresh credential: stage + clear called with the
  fresh tokens, scoped to (container-owner, connector).
- `/poll` for an un-provisioned owner: helper no-ops (no raise) — onboarding's
  first connect still works.
- `_hot_swap_provider` (switch into chatgpt_oauth) still stages + clears.
- Gate: `_has_oauth_tokens` returns true on `state=active` **without** decrypting
  the blob (assert the connected-check no longer goes through `get_decrypted_tokens`).
- Regression intent test: simulate "container already has a live token row + a
  reprovision" → assert the row is **not** cleared.

## Deferred / out of scope (follow-ups, not blockers)

- **"Blank the consumed seed":** after the container confirms it imported the seed,
  blank `encrypted_tokens` (keep `state=active` as the connected flag). Honors
  "don't store known-stale data," but requires a reliable *container-reports-imported*
  signal — otherwise blanking risks a restart-before-import booting unauthenticated.
  Revisit once such a signal exists. The blob is Fernet-encrypted and *spent*, so
  the residual exposure is ~nil; the inert seed left by this fix is functionally
  harmless.
- **Approach C (sync rotated tokens back to DDB):** make DDB a faithful mirror.
  Rejected as the primary fix (chases a value the container owns; always has a
  rotation→sync window). Possible future defense-in-depth.
- **Google/`gog` OAuth audit:** confirm whether Google OAuth materialization shares
  the stage+clear mechanism and the same staleness risk. Out of scope here; audit
  separately.

## References

- Memory `project_openclaw_oauth_auth_model` (root-cause history; 06-23 update).
- `core/containers/ecs_manager.py` — `_pre_stage_oauth_for_user` (1426),
  callers (1518, 2269 via `write_user_configs`).
- `core/services/billing_service.py` — `_hot_swap_provider` (781), stage+clear
  (839/849), provider-change gate (731).
- `routers/oauth.py` — `/poll` (146), `/disconnect` (187).
- `core/containers/workspace.py` — `pre_stage_auth_profile_store` (727),
  `clear_default_agent_auth_profile_row` (908).
- `core/services/provision_gate.py` — `_has_oauth_tokens` (138).
- `core/services/oauth_service.py` — `get_decrypted_tokens` (399), row `state`
  machine (`pending` → `active`).
