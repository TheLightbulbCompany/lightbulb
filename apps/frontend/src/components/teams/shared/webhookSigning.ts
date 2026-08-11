// Webhook signing-mode constants shared by every "add a webhook trigger" UI —
// today the detail panel's TriggerDialog (RoutineDetailPanel.tsx) and the
// create dialog's "Any other app" rung (RoutinesPanel.tsx).

// Signing modes Paperclip's createRoutineTriggerSchema accepts
// (paperclip/packages/shared/src/constants.ts ROUTINE_TRIGGER_SIGNING_MODES).
// Only bearer/hmac_sha256 carry a replay window — github_hmac verifies via
// GitHub's own delivery-id scheme and "none" has no signature to replay-guard.
export const SIGNING_MODE_OPTIONS: ReadonlyArray<readonly [string, string]> = [
  ["bearer", "Bearer token"],
  ["hmac_sha256", "HMAC (SHA-256)"],
  ["github_hmac", "GitHub HMAC"],
  ["none", "None (unsigned)"],
];
export const REPLAY_WINDOW_SIGNING_MODES = new Set(["bearer", "hmac_sha256"]);
export const DEFAULT_REPLAY_WINDOW_SEC = 300;
