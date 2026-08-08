// apps/frontend/src/components/teams/issues/lib/hideIssue.ts
//
// Hiding a task stamps `hiddenAt`, and Paperclip's issue reads apply
// `visibleIssueCondition()` — `hidden_at IS NULL AND harness_kind IS NULL` —
// unconditionally. `IssueFilters` carries no include-hidden escape hatch, so
// there is no query that can surface a hidden task again and no list to
// restore it from. The PATCH itself is reversible (`hiddenAt: null` clears the
// stamp) but only if the caller still holds the id.
//
// That makes the undo toast the sole route back, which is why both Hide call
// sites (the Tasks row menu and the task-detail overflow menu) go through
// here rather than firing the PATCH themselves.

import { notifySuccess, notifyApiError } from "@/lib/notify";

/** Long enough to actually reach for — the default 4s is too tight when the
 *  toast is the only way to undo. */
const UNDO_TOAST_MS = 12_000;

export type HideIssuePatch = (
  issueId: string,
  patch: { hiddenAt: string | null },
) => Promise<unknown> | unknown;

/**
 * Hide a task and offer an Undo.
 *
 * `update` is whatever the host already uses to PATCH an issue, so this works
 * against both the panel's `onUpdateIssue` and the detail page's mutation hook.
 * `onHidden` runs only after the hide succeeds (the detail page uses it to
 * leave the now-invisible task).
 */
export async function hideIssueWithUndo(
  issueId: string,
  update: HideIssuePatch,
  onHidden?: () => void,
): Promise<void> {
  try {
    await update(issueId, { hiddenAt: new Date().toISOString() });
  } catch (e) {
    // Surface it — a silent failure here reads as "hidden" while the task is
    // still sitting in the list.
    notifyApiError(e, "Couldn't hide the task. Try again.");
    return;
  }
  onHidden?.();
  notifySuccess("Task hidden.", {
    durationMs: UNDO_TOAST_MS,
    action: {
      label: "Undo",
      onClick: () => {
        void Promise.resolve(update(issueId, { hiddenAt: null })).catch((e) =>
          notifyApiError(e, "Couldn't restore the task."),
        );
      },
    },
  });
}
