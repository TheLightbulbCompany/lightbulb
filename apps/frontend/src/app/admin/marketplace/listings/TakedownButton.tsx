"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

import { takedownListing } from "@/app/admin/_actions/marketplace";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Reason = "dmca" | "policy" | "fraud" | "seller-request";

const REASONS: ReadonlyArray<{ value: Reason; label: string }> = [
  { value: "policy", label: "Platform policy violation" },
  { value: "dmca", label: "DMCA / IP claim" },
  { value: "fraud", label: "Fraud / impersonation" },
  { value: "seller-request", label: "Seller request" },
];

const MIN_BASIS_LENGTH = 10;
const MAX_BASIS_LENGTH = 4096;
const MAX_WRONG_ATTEMPTS = 3;

export interface TakedownButtonProps {
  listingId: string;
  listingName: string;
  slug: string;
}

/**
 * Destructive admin action: take down a published listing.
 *
 * Opens a dialog with three controls — a `<select>` for the structured
 * reason, a textarea for the free-text basis (10-4096 chars; recorded on
 * the audit log), and a typed-confirmation input. The typed phrase is
 * `takedown <slug>` matching the CEO S5 convention used by other
 * destructive admin surfaces. Calls `takedownListing` (which posts to
 * `/admin/marketplace/listings/{id}/takedown`) and `router.refresh()`s on
 * success so the listing's new `taken_down` status renders.
 *
 * Implementation differs from <ConfirmActionDialog/> because we need three
 * inputs (reason, basis, typed-phrase) — that primitive only models the
 * typed-phrase. Lockout behavior (3 wrong typed-phrase attempts) mirrors
 * <ConfirmActionDialog/> so the destructive surface area stays consistent.
 */
export function TakedownButton({ listingId, listingName, slug }: TakedownButtonProps) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<Reason>("policy");
  const [basis, setBasis] = useState("");
  const [typed, setTyped] = useState("");
  const [wrongAttempts, setWrongAttempts] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mismatch, setMismatch] = useState(false);

  const confirmText = `takedown ${slug}`;
  const locked = wrongAttempts >= MAX_WRONG_ATTEMPTS;
  const matchesTyped = typed === confirmText;
  const basisLen = basis.trim().length;
  const basisValid = basisLen >= MIN_BASIS_LENGTH && basisLen <= MAX_BASIS_LENGTH;
  const submitDisabled = locked || busy || typed.length === 0 || !basisValid;

  function handleOpenChange(next: boolean) {
    if (busy) return;
    setOpen(next);
    if (!next) {
      // Reset transient state on close. Lockout persists for component lifetime.
      setReason("policy");
      setBasis("");
      setTyped("");
      setError(null);
      setMismatch(false);
    }
  }

  async function handleSubmit(event: React.MouseEvent<HTMLButtonElement>) {
    event.preventDefault();
    if (locked || busy) return;
    if (!matchesTyped) {
      // Say so: a silent miss let an admin burn all three attempts into the
      // lockout without ever learning the phrase was wrong.
      setWrongAttempts((n) => n + 1);
      setMismatch(true);
      return;
    }
    setMismatch(false);
    setBusy(true);
    setError(null);
    try {
      const result = await takedownListing(listingId, reason, basis.trim());
      if (!result.ok) {
        setError(result.error ?? `takedown_failed_${result.status}`);
        return;
      }
      setOpen(false);
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <AlertDialog open={open} onOpenChange={handleOpenChange}>
        <AlertDialogTrigger asChild>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="border-err text-err hover:bg-accent hover:text-err"
          >
            Take down listing
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Take down {listingName}</AlertDialogTitle>
            <AlertDialogDescription>
              This revokes every license for this listing, flips its status
              to <span className="font-mono">taken_down</span>, and is
              immediate. Pick a reason and explain why; both are recorded on
              the audit log.
            </AlertDialogDescription>
          </AlertDialogHeader>

          {locked ? (
            <p
              role="alert"
              className="rounded-md border-l-2 border-warn bg-card px-3 py-2 text-sm text-warn"
            >
              Locked. Reload the page to try again.
            </p>
          ) : (
            <div className="space-y-3">
              <div className="space-y-1">
                <label
                  htmlFor="takedown-reason"
                  className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
                >
                  Reason
                </label>
                <select
                  id="takedown-reason"
                  value={reason}
                  onChange={(e) => setReason(e.target.value as Reason)}
                  disabled={busy}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
                >
                  {REASONS.map((r) => (
                    <option key={r.value} value={r.value}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="space-y-1">
                <label
                  htmlFor="takedown-basis"
                  className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
                >
                  Basis ({MIN_BASIS_LENGTH}-{MAX_BASIS_LENGTH} chars)
                </label>
                <textarea
                  id="takedown-basis"
                  value={basis}
                  onChange={(e) => setBasis(e.target.value)}
                  placeholder="What about this listing required takedown?"
                  rows={4}
                  disabled={busy}
                  maxLength={MAX_BASIS_LENGTH}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground"
                  aria-label="Takedown basis (recorded on audit log)"
                />
                <p className="text-xs text-muted-foreground">
                  {basisLen} / {MAX_BASIS_LENGTH}
                  {basisLen > 0 && basisLen < MIN_BASIS_LENGTH && (
                    <span className="text-warn">
                      {" "}
                      — at least {MIN_BASIS_LENGTH} characters required
                    </span>
                  )}
                </p>
              </div>

              <div className="space-y-1">
                <label
                  htmlFor="takedown-confirm"
                  className="text-xs font-medium uppercase tracking-caps text-muted-foreground"
                >
                  Type{" "}
                  <span className="font-mono font-semibold text-foreground">
                    {confirmText}
                  </span>{" "}
                  to confirm
                </label>
                <Input
                  id="takedown-confirm"
                  value={typed}
                  onChange={(e) => setTyped(e.target.value)}
                  placeholder={confirmText}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy}
                  aria-label={`Type ${confirmText} to confirm`}
                />
              </div>
            </div>
          )}

          {mismatch && !locked && (
            <p role="alert" className="text-xs text-err">
              That doesn&apos;t match. {MAX_WRONG_ATTEMPTS - wrongAttempts} attempt
              {MAX_WRONG_ATTEMPTS - wrongAttempts === 1 ? "" : "s"} left before this locks.
            </p>
          )}
          {error && (
            <p role="alert" className="text-xs text-err">
              {error}
            </p>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <Button
              type="button"
              onClick={handleSubmit}
              disabled={submitDisabled}
              aria-busy={busy}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {busy ? "Working…" : "Take down listing"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
