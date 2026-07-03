"use client";

import * as React from "react";

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
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface ConfirmActionDialogProps {
  /** The exact string the user must type to enable confirmation. */
  confirmText: string;
  /** Human-readable label for the destructive/sensitive action, used in the heading and ARIA label. */
  actionLabel: string;
  /** When true, the confirm button uses destructive (red) styling. */
  destructive?: boolean;
  /** Async callback fired after the user types the correct confirmation string and clicks confirm. */
  onConfirm: () => Promise<void> | void;
  /** Trigger element (typically a button) that opens the dialog. */
  children: React.ReactNode;
}

const MAX_WRONG_ATTEMPTS = 3;

/**
 * Confirmation dialog requiring the operator to type a literal phrase before
 * a destructive action can fire. Implements the CEO S5 lockout policy:
 * after 3 wrong attempts, the dialog locks until the page is reloaded.
 */
export function ConfirmActionDialog({
  confirmText,
  actionLabel,
  destructive = false,
  onConfirm,
  children,
}: ConfirmActionDialogProps) {
  const [open, setOpen] = React.useState(false);
  const [typed, setTyped] = React.useState("");
  const [wrongAttempts, setWrongAttempts] = React.useState(0);
  const [busy, setBusy] = React.useState(false);

  const locked = wrongAttempts >= MAX_WRONG_ATTEMPTS;
  const matches = typed === confirmText;
  // Confirm is enabled once any text is typed so a wrong submission counts
  // toward the 3-attempt lockout; matches-only enabling would make the
  // lockout unreachable. The click handler validates the value.
  const confirmDisabled = locked || busy || typed.length === 0;

  // Reset typed/error state on close (lockout persists for component lifetime).
  function handleOpenChange(next: boolean) {
    if (busy) return;
    setOpen(next);
    if (!next) {
      setTyped("");
    }
  }

  async function handleConfirm(event: React.MouseEvent<HTMLButtonElement>) {
    // Always prevent the default Radix close-on-action; we manage open state
    // manually so async work can finish before the dialog disappears.
    event.preventDefault();

    if (locked || busy) return;

    if (!matches) {
      setWrongAttempts((n) => n + 1);
      return;
    }

    setBusy(true);
    try {
      await onConfirm();
      setOpen(false);
      setTyped("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogTrigger asChild>{children}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{actionLabel}</AlertDialogTitle>
          <AlertDialogDescription>
            Type{" "}
            <span className="font-mono font-semibold text-white">
              {confirmText}
            </span>{" "}
            below to confirm.
          </AlertDialogDescription>
        </AlertDialogHeader>

        {locked ? (
          <p
            role="alert"
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-200"
          >
            Locked. Reload the page to try again.
          </p>
        ) : (
          <Input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={confirmText}
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            aria-label={`Type ${confirmText} to confirm`}
          />
        )}

        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={handleConfirm}
            disabled={confirmDisabled}
            aria-busy={busy}
            aria-label={`Confirm ${actionLabel}`}
            className={cn(
              destructive &&
                "bg-destructive text-white hover:bg-destructive/90 focus-visible:ring-destructive/20",
            )}
          >
            {busy ? "Working\u2026" : actionLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
