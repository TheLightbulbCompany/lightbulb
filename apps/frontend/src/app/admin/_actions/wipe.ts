"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the admin account-wipe surface
 * (docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md).
 *
 * Two calls, mirroring the backend orchestrator 1:1:
 *   - `previewAccountWipe` -> `POST /admin/users/{id}/wipe/preview` (read-only
 *     dry run; deletes nothing).
 *   - `executeAccountWipe` -> `POST /admin/users/{id}/wipe` with
 *     `{ confirm_email }` (the actual teardown; the backend re-verifies the
 *     email server-side against the account's row on file — the client match
 *     in <WipeAccountDialog> is UX only, not the safety boundary).
 *
 * Both return the same `WipeReport` shape
 * (`core/services/account_wipe.py WipeReport.to_dict()`): preview steps carry
 * `status: "would_delete" | "skipped"`, execute steps carry
 * `status: "ok" | "skipped" | "failed"`. A 400 surfaces as
 * `result.error === "org_wipe_unsupported"` (org owners aren't supported in
 * v1) or `"email_mismatch"` (execute only, typed email didn't match).
 */

export type WipeStepStatus = "ok" | "skipped" | "failed" | "would_delete";

export interface WipeStep {
  system: string;
  action: string;
  status: WipeStepStatus;
  detail: string;
  error: string | null;
}

export interface WipeReport {
  owner_id: string;
  dry_run: boolean;
  steps: WipeStep[];
}

export async function previewAccountWipe(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/wipe/preview`);
}

export async function executeAccountWipe(
  userId: string,
  confirmEmail: string,
): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/wipe`, {
    confirm_email: confirmEmail,
  });
}
