# Plan: Enable channels only when configured + clean up orphaned channel creds

**Date:** 2026-06-05
**Status:** Draft for review (no code written yet)
**Driver:** Co-founder cannot set up Telegram; prod container emits Slack `invalid_auth` crash-loop logs for a channel that was "never set up."
**Evidence:** `docs/superpowers/research/2026-06-05-channels-audit-5.28.md` §0 (corrected against live prod CloudWatch 2026-06-06). Memory `project_channels_enabled_orphan_crashloop`.

## Ground truth (verified against prod, image `2026.5.28-1ad2085`)

1. **Provisioning ships all 3 channels `enabled: true` with no accounts** (`core/containers/config.py:793-816`). For telegram/discord with no provider-level token this is harmless-idle. For Slack, **orphaned provider-level `botToken`/`appToken`** (legacy, from an older wizard that wrote tokens at the provider level instead of under `accounts`) make OpenClaw run a `[default]` account that fails `auth.test` → `invalid_auth` → auto-restart 1→10 every ~5 min, forever. Confirmed in `/isol8/prod/openclaw`.
2. **No delete path clears provider-level tokens.** `admin_delete_bot` (`routers/channels.py:256-259`) deletes only `channels.<provider>.accounts.<id>`. The current wizard only ever writes under `accounts.<agentId>` (`BotSetupWizard.tsx:382-389`), so the orphans are legacy and un-reachable by any UI.
3. **Telegram setup failure = invalid token (`getMe` 401)** *plus* a deferral trap: the channel reload is deferred while the busy 0.5-vCPU box has operations/embedded runs active (`[reload] ... deferring until N operation(s), M embedded run(s) complete`), so the account doesn't attempt `getMe` until **after** the wizard's 60s timeout (`BotSetupWizard.tsx:399-429`) has already rolled it back. The user sees the generic *"never reported ready, check the token"* and never sees the real 401.
4. **Upstream fact (verified `src/gateway/config-reload-plan.ts:157-182,324-326`):** a `channels.<id>` change (`enabled` + `accounts`) is classified `kind:"hot"` → `restart-channel:<id>` (per-channel hot restart, seconds), **not** a gateway restart. The `config.py:783-788` comment ("first enable → ~6 min gateway restart") is overstated for the bundled telegram/slack/discord, which load at boot regardless of `enabled`. **→ shipping channels `enabled:false`-until-configured is cheap.**

## Design principles

- **A channel is `enabled` iff it has ≥1 valid configured account.** No account ⇒ `enabled:false` ⇒ plugin loaded but no provider started ⇒ no auth, no logs, no CPU.
- **Tokens live ONLY under `accounts.<agentId>`.** Provider-level token fields are never written and are actively stripped when found.
- **Every openclaw.json-touching change is validated against the genuine 2026.5.28 schema before it can fleet-deploy** (memory `feedback_verify_full_config_against_upstream_schema`; an invalid value boot-loops the container — `reference_openclaw_config_brick`).
- **TDD throughout.** Write the failing test first; per `feedback_run_tests_at_end`, run targeted tests per task and the full suite once at the end.

## Pre-flight (must do before writing Piece 1)

- [ ] On a **dev** container, confirm `channels.slack.enabled:false` still LOADS the bundled slack plugin (so a later first account-add is a hot per-channel restart, not a gateway restart). Verify by: set enabled:false, reprovision/boot, check the boot log lists `slack` among plugins, then add an account and confirm a `restart-channel:slack` hot reload (not a gateway restart). This validates principle (4) empirically before we rely on it.
- [ ] Snapshot the current dev openclaw.json channels block as a test fixture.

## Piece 1 — Provision channels disabled-until-configured

**Files:** `core/containers/config.py` (the `channels` block ~793-816); tests in `tests/unit/containers/test_config.py`.

- **Test first:** `write_openclaw_config` with no channel accounts emits `channels.{telegram,discord,slack}` with `enabled: False` (or omits the per-channel block — pick one and assert it). Assert no provider-level token keys are ever emitted.
- **Implement:** ship each channel `{"enabled": False, "dmPolicy": "pairing"}`. Keep the `webchat`-exclusion comment. Update the `783-792` comment to reflect the verified hot-reload behavior (cite `config-reload-plan.ts`), not the stale "gateway restart" claim.
- **Schema-validate:** assert the emitted config passes the 2026.5.28 channel schema (reuse/extend whatever harness the `embeddedHarness`/agents-defaults work used; if none, add a minimal validator fixture from the upstream generated schema).

## Piece 2 — Wizard add flips `enabled:true`; delete strips ALL creds + flips `enabled:false` when empty

**Files:** `routers/channels.py` (`admin_delete_bot` :229-284), the wizard rollback (`BotSetupWizard.tsx:rollbackAccount` / submit :382-389), and a shared backend helper.

- **Add path:** the wizard already PATCHes `{enabled:true, accounts:{[id]:cfg}}` (`BotSetupWizard.tsx:382-389`) — keep. (Optional: move the `enabled:true` flip server-side so the client can't desync it.)
- **Delete path (test first):** after deleting `accounts.<id>`, if `channels.<provider>.accounts` is now empty, ALSO: delete provider-level `botToken`/`appToken`/`token`/`signingSecret`/`mode`, and set `channels.<provider>.enabled = false`. Tests: (a) deleting the last account disables the channel and strips provider creds; (b) deleting one of two accounts leaves the channel enabled and the other account intact.
- Mirror the same teardown in the wizard's `rollbackAccount` (the 60s-timeout / error path) so a failed setup never leaves a half-configured channel enabled.

## Piece 3 — One-time orphan cleanup for existing containers

**Files:** `apps/backend/scripts/cleanup_orphan_channel_creds.py` (new), plus optionally fold into the existing reconcile/`strip_removed_agent_keys` pattern (`config_patcher.py:569`).

- Idempotent script (dry-run default, `--apply`): for each container, for each channel, if `accounts` is empty/absent but provider-level token fields exist → strip them and set `enabled:false`. Paginate the `containers` table like `backfill_paperclip_for_legacy_users.py`.
- **Already done manually on the founders' prod box** (org `org_3DBS8G83oPrMm49yN2b70EjoFpN`, 2026-06-06: stripped `slack.botToken`/`appToken`/`mode`, set `slack.enabled:false`, `.bak` saved). The script generalizes this so reprovisions/other containers don't reintroduce it.

## Piece 4 — Wizard surfaces the real failure instead of a blind 60s timeout

**Files:** `BotSetupWizard.tsx:399-431`; possibly a `channels.status` field passthrough in `routers/channels.py`/the gateway proxy.

- **Surface auth failures:** when the polled `channels.status` account shows `lastError` containing `getMe`/`401`/`invalid_auth`/`unauthorized`, show that verbatim (e.g. *"Telegram rejected this token (401). Get a fresh token from @BotFather."*) and stop polling early — instead of waiting out the 60s.
- **Distinguish "container busy / reload deferred" from "bad token":** if `channels.status` reports the account as pending/`reloadDeferred` (or `lastStartAt` never advances) within the window, show *"Your container is busy finishing other work; still setting up…"* and either extend the wait or let the user keep it pending — do NOT auto-rollback with a token-blaming message.
- Tests (frontend unit): given a mocked `channels.status` returning a `getMe 401` lastError → asserts the actionable error + no false "check the token" generic; given a never-advancing `lastStartAt` → asserts the busy state, not the token error.
- **Dependency:** the deferral itself is driven by CPU starvation from the MCP connect-storm crash-loop (`project_openclaw_mcp_connect_storm_crashloop`). Piece 1 (killing the idle Slack loop) reduces it; the deeper fix is that separate workstream. Note the coupling; don't try to fix the MCP storm here.

## Piece 5 — (Latent, separate) Slack manifest scopes 6 → 23

**File:** `BotSetupWizard.tsx:55-75` (`SLACK_APP_MANIFEST`).

- Not the current log issue (the app is dead; the manifest never engages). Becomes relevant only once a **valid** Slack app is connected. Replace with the 5.28 canonical manifest from `extensions/slack/src/setup-shared.ts:10-105` (adds `app_home` + `assistant_view`, `/openclaw` command, ~17 scopes, ~13 events). Do as a follow-up when Slack is actually re-set-up.

## Rollout / verification

1. Pre-flight check (above) passes on dev.
2. Implement Pieces 1-4 on a branch; targeted tests per piece, full backend + frontend suite at the end.
3. Validate emitted config against the 2026.5.28 schema in CI/test.
4. Deploy to dev; reprovision a dev container; confirm: no channel runs until configured; setting up a channel with a bad token shows the actionable error (not a 60s hang); deleting a channel strips creds + disables it; no `invalid_auth`/idle-channel logs.
5. PR per `feedback_always_pr_never_direct_main`; bundle Pieces 1-4 (one coherent fix) per `feedback_bundle_related_fixes`; Piece 5 separate.
6. After merge: run the Piece 3 cleanup script across the fleet; reprovision the founders' container onto the clean config.

## Open questions

- Piece 1: emit `enabled:false` vs omit the per-channel block entirely? (Both must pass the 5.28 validator; `enabled:false` is more explicit and keeps `dmPolicy`.) Decide after the pre-flight.
- Should `enabled` be flipped server-side on add (authoritative) rather than trusting the client PATCH? Leaning yes for robustness.
