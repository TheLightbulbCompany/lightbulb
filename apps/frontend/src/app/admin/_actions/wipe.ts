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
 * `status: "ok" | "skipped" | "failed"`, and either can carry `"refused"` —
 * a guard kept the resource (a workspace member's shared Paperclip company).
 * When `shared_company` is set, execute also needs `confirm_workspace` equal to
 * the workspace name (its org id when Clerk has no name). A 400 surfaces as
 * `result.error === "org_wipe_unsupported"` (org owners aren't supported in
 * v1), `"email_mismatch"`, or `"workspace_mismatch"` (execute only).
 */

export type WipeStepStatus = "ok" | "skipped" | "failed" | "would_delete" | "refused";

export interface WipeStep {
  system: string;
  action: string;
  status: WipeStepStatus;
  detail: string;
  error: string | null;
}

export interface SharedCompany {
  company_id: string;
  org_id: string;
  workspace_name: string | null;
  owner_user_id: string | null;
  owner_email: string | null;
  member_count: number;
}

export interface WipeReport {
  owner_id: string;
  dry_run: boolean;
  shared_company: SharedCompany | null;
  steps: WipeStep[];
}

export async function previewAccountWipe(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/wipe/preview`);
}

export async function executeAccountWipe(
  userId: string,
  confirmEmail: string,
  confirmWorkspace?: string,
): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/wipe`, {
    confirm_email: confirmEmail,
    confirm_workspace: confirmWorkspace,
  });
}
