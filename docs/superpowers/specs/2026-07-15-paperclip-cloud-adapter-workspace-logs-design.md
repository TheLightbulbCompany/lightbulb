# Paperclip cloud-adapter workspace log reporting

**Date:** 2026-07-15
**Repo:** `Isol8AI/paperclip` fork (`~/Desktop/paperclip-fork`, branch off `isol8/2026.6-circuit-breaker`)
**Isol8-side change:** none beyond the eventual `paperclip-version.json` digest bump

## Problem

Every issue-assigned run for an `openclaw_gateway` (cloud) agent prints two
misleading lines into the run transcript, which the Isol8 Teams run-detail
dashboard renders verbatim:

1. `[paperclip] No project or prior session workspace was available. Using
   fallback workspace "/paperclip/instances/default/workspaces/<agentId>" for
   this run.` — emitted by the workspace resolver's final fallback branch
   (`server/src/services/heartbeat.ts:4504`). It fires on **every** Isol8 run
   because (a) Isol8 issues have no Paperclip project workspace configured and
   (b) issue-assigned wakes reset the task session, so no prior session cwd
   exists. For cloud adapters the resolved local cwd is bookkeeping only: the
   adapter never transmits it, and the agent actually executes in its own
   workspace on the user's container (`/home/node/.openclaw/workspaces/{agentId}`
   on EFS, set by Isol8's `write_openclaw_config`).

2. `[paperclip] Skipping saved session resume for task "<key>" because wake
   reason is issue_assigned.` — emitted unconditionally whenever the wake
   reason is a reset reason (`heartbeat.ts:8625-8631`), even when no saved
   session existed to skip. Brand-new issues log it too.

The dashboard therefore implies the agent is working in a meaningless local
fallback directory and skipping sessions that never existed. Legibility bug,
not a correctness bug.

## Decision

Fork-only fix. No new adapterConfig field, no fleet backfill — the remote
workspace path is derivable inside the fork, so all existing agents are fixed
by the next image bump.

## Changes (all in `server/src/services/heartbeat.ts` + small helpers)

### 1. Remote-workspace line replaces the fallback warnings

Add a small `adapterExecutesRemotely(adapterType)` helper (returns true for
`openclaw_gateway`). Keep the check narrow and local so the fork diff stays
surgical for upstream rebases.

When the agent's adapter executes remotely:

- Workspace resolution mechanics are **unchanged**: the agent-home dir is
  still created and used as the local bookkeeping cwd (Paperclip needs a
  stat-able local path).
- All "not available / fallback workspace" warnings are suppressed.
- One informative line is emitted instead:

  ```
  [paperclip] Cloud adapter run — executing in remote workspace
  "/home/node/.openclaw/workspaces/<agentId>" on the agent's container.
  ```

- The remote path is recorded on `context.paperclipWorkspace.remoteCwd` so a
  future run-detail UI field can render it (the current RunDetailPanel shows
  no workspace metadata; the transcript is the only visible surface today).

**Remote path derivation:** `dirname(adapterConfig.claimedApiKeyPath)` when
present (Isol8 sends `~/.openclaw/workspaces/{agentId}/paperclip-claimed-api-key.json`),
falling back to the `~/.openclaw/workspaces/{agentId}` convention using the
Paperclip agent id (which keys the OpenClaw workspace — see isol8
`core/containers/config.py:717`). Derivation is display-only and wrapped so a
malformed adapterConfig can never fail a run.

### 2. Skip-resume line gated on an actual saved session

Emit `Skipping saved session resume …` only when a saved task session existed
(`taskSession != null`). The reset **behavior** is untouched — issue-assigned,
review/approval/changes-requested, and timer wakes still start fresh sessions
per `shouldResetTaskSessionForWake`. Only the log line is gated, for all reset
reasons.

## Out of scope

- `[openclaw-gateway] transient error, retry 1/2 … (connect)` lines stay —
  real signal (cold container wake; dev containers scale to zero).
- No Isol8 backend/frontend changes.
- No change to which cwd is persisted/leased locally.

## Testing

Follow the fork's existing exported-pure-helper pattern (like
`shouldResetTaskSessionForWake` / `describeSessionResetReason`), with vitest
coverage in `server/src/__tests__/`:

- skip-resume log gating: saved session present → logs; absent → silent;
  reset behavior identical in both cases.
- warning suppression: `openclaw_gateway` → no fallback warnings, one remote
  line; local/process adapters → unchanged warnings.
- remote-cwd derivation: `claimedApiKeyPath` present, absent, and malformed
  (falls back to convention; never throws).

## Ship path

1. Branch off `isol8/2026.6-circuit-breaker` in `~/Desktop/paperclip-fork`,
   PR against the fork.
2. Tag `v2026.6-isol8.<n+1>` → fork CI builds `ghcr.io/isol8ai/paperclip`.
3. Isol8 PR: bump digest in `paperclip-version.json` (dev tag first) — this
   spec rides that PR.
4. `build-paperclip-image.yml` re-tags into ECR; redeploy dev tenant; verify a
   fresh issue run's transcript on the dev dashboard shows the remote-workspace
   line and no fallback/skip-resume noise.
5. Promote prod tag.
