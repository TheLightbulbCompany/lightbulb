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
 * `result.error === "org_wipe_unsupported"` (org owners aren't supported by the
 * USER wipe — use the workspace wipe below), `"email_mismatch"`, or
 * `"workspace_mismatch"` (execute only).
 *
 * Workspace wipe (org-keyed, `core/services/workspace_wipe.py`):
 *   - `previewWorkspaceWipe` -> `POST /admin/workspaces/{orgId}/wipe/preview`
 *     (read-only; billing shown preserved/skipped unless `includeBilling`).
 *   - `executeWorkspaceWipe` -> `POST /admin/workspaces/{orgId}/wipe` with
 *     `{ confirm_org, layers?, include_billing?, confirm_billing?, delete_shared_members?,
 *     preview_fingerprint }`. `confirm_org` must equal the org id;
 *     `preview_fingerprint` is required and must match the preview confirmed with
 *     the SAME options (else 409 `stale_preview`); billing moves only with
 *     `include_billing` + `confirm_billing: "DELETE-BILLING"`.
 *   - The report's `members[]` says per person `delete_account` or
 *     `remove_membership` (they belong to `other_orgs` too). Shared members keep
 *     their account unless `deleteSharedMembers`.
 *   - Errors are `"<code>: <message>"`; `result.code` carries the code:
 *     `stale_preview`, `workspace_mismatch`, `workspace_wipe_refused` (vendor org,
 *     missing fingerprint, acting admin is a member, …), `bootstrap_account_protected`.
 *   - Audit action is `account_wipe_workspace` (not `account_wipe`).
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

export interface WorkspaceWipeMember {
  user_id: string;
  email: string;
  action: "delete_account" | "remove_membership";
  other_orgs: string[];
}

export interface WorkspaceWipePreview {
  layers?: string[];
  includeBilling?: boolean;
  deleteSharedMembers?: boolean;
}

export interface WorkspaceWipeConfirm extends WorkspaceWipePreview {
  confirmOrg: string;
  confirmBilling?: string;
  previewFingerprint: string;
}

export type WorkspaceWipeResult = ActionResult & { code?: string };

function withCode(res: ActionResult): WorkspaceWipeResult {
  return res.ok ? res : { ...res, code: res.error?.split(": ")[0] };
}

export async function previewWorkspaceWipe(orgId: string, opts?: WorkspaceWipePreview): Promise<WorkspaceWipeResult> {
  return withCode(
    await adminPost(`/admin/workspaces/${encodeURIComponent(orgId)}/wipe/preview`, {
      layers: opts?.layers,
      include_billing: opts?.includeBilling ?? false,
      delete_shared_members: opts?.deleteSharedMembers ?? false,
    }),
  );
}

export async function executeWorkspaceWipe(orgId: string, confirm: WorkspaceWipeConfirm): Promise<WorkspaceWipeResult> {
  return withCode(
    await adminPost(`/admin/workspaces/${encodeURIComponent(orgId)}/wipe`, {
      confirm_org: confirm.confirmOrg,
      layers: confirm.layers,
      include_billing: confirm.includeBilling ?? false,
      confirm_billing: confirm.confirmBilling,
      delete_shared_members: confirm.deleteSharedMembers ?? false,
      preview_fingerprint: confirm.previewFingerprint,
    }),
  );
}
