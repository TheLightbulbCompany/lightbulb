"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import {
  approveListing,
  rejectListing,
} from "@/app/admin/_actions/marketplace";
import { ConfirmActionDialog } from "@/components/admin/ConfirmActionDialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

export interface ModerationActionsProps {
  listingId: string;
  listingName: string;
  slug: string;
  /**
   * Listing version to action on. The review queue is multi-version
   * (publish_v2 flow), so this MUST come from the review row — calling
   * approve/reject without it falls through to backend default version=1
   * and either targets the wrong row or 409s for v2+ items.
   */
  version: number;
  /**
   * For publish_v2: the previously-published version to retire when this
   * one is approved. Triggers the atomic flip on backend. Omit for
   * first-publish (v1) listings.
   */
  prevVersion?: number;
  /**
   * Optional starter copy for the reject-notes textarea. Used by the
   * listing detail page to pre-fill `marketplace_safety` high-severity
   * findings; admins can edit before sending. Empty/undefined preserves
   * the original blank-textarea behavior used on the queue page.
   */
  prefilledRejectionNotes?: string;
  /** When true, the Approve button is disabled — used when the preview
   * couldn't fully scan the artifact. Reject + Takedown remain enabled
   * because they're still safe to take on a partial preview. */
  approveDisabled?: boolean;
  /** Tooltip / aria copy shown when Approve is disabled. */
  approveDisabledReason?: string;
}

/**
 * Approve / reject button cluster for a single listing in the review queue.
 *
 * Approve uses the shared `ConfirmActionDialog` (CEO S5 typed-confirmation
 * convention — operator types `approve <slug>`). Reject is a sibling
 * AlertDialog with a free-form `notes` field instead of a typed phrase, since
 * the rejection notes are surfaced to the seller and recorded on the
 * moderation audit log; gating it behind a typed phrase would add no signal
 * over the notes themselves. Both surfaces call the moderation server actions
 * and `router.refresh()` on success so the queue page re-runs and the just-
 * actioned listing drops out of the list. Errors render inline; the page
 * itself has no error slot.
 */
export function ModerationActions({
  listingId,
  listingName,
  slug,
  version,
  prevVersion,
  prefilledRejectionNotes,
  approveDisabled,
  approveDisabledReason,
}: ModerationActionsProps) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [notes, setNotes] = useState(prefilledRejectionNotes ?? "");
  const [busy, setBusy] = useState(false);

  async function handleApprove() {
    setError(null);
    const result = await approveListing(listingId, { version, prevVersion });
    if (!result.ok) {
      setError(result.error ?? `approve_failed_${result.status}`);
      return;
    }
    router.refresh();
  }

  async function handleReject(event: React.MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    if (busy) return;
    const trimmed = notes.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    try {
      const result = await rejectListing(listingId, trimmed, { version });
      if (!result.ok) {
        setError(result.error ?? `reject_failed_${result.status}`);
        return;
      }
      setRejectOpen(false);
      setNotes(prefilledRejectionNotes ?? "");
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  function handleRejectOpenChange(next: boolean) {
    if (busy) return;
    setRejectOpen(next);
    if (!next) {
      // On close, restore the prefilled notes (don't clobber the safety-scan
      // pre-fill if the admin opened-then-cancelled).
      setNotes(prefilledRejectionNotes ?? "");
    }
  }

  return (
    <div className="flex shrink-0 flex-col items-end gap-2">
      <div className="flex gap-2">
        {approveDisabled ? (
          <Button
            type="button"
            size="sm"
            disabled
            title={approveDisabledReason}
            aria-label={approveDisabledReason ?? "Approve disabled"}
            data-testid="approve-disabled"
            className="bg-zinc-700/20 text-zinc-500 cursor-not-allowed"
          >
            Approve
          </Button>
        ) : (
          <ConfirmActionDialog
            confirmText={`approve ${slug}`}
            actionLabel={`Approve ${listingName}`}
            onConfirm={handleApprove}
          >
            <Button
              type="button"
              size="sm"
              className="bg-green-700/30 text-green-300 hover:bg-green-700/40"
            >
              Approve
            </Button>
          </ConfirmActionDialog>
        )}

        <AlertDialog open={rejectOpen} onOpenChange={handleRejectOpenChange}>
          <AlertDialogTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="border-red-700/40 bg-red-700/20 text-red-300 hover:bg-red-700/30"
            >
              Reject
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Reject {listingName}</AlertDialogTitle>
              <AlertDialogDescription>
                Provide rejection notes. These will be visible to the seller
                and recorded on the moderation audit log.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Reason for rejection (required)…"
              rows={4}
              disabled={busy}
              className="w-full rounded-md border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-zinc-500 focus:outline-none"
              aria-label="Rejection notes"
            />
            <AlertDialogFooter>
              <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
              <AlertDialogAction
                onClick={handleReject}
                disabled={busy || notes.trim().length === 0}
                className="bg-destructive text-white hover:bg-destructive/90"
              >
                {busy ? "Rejecting…" : "Reject listing"}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
      {error && (
        <span className="text-xs text-red-400" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
