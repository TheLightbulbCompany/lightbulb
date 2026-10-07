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
  const [mismatchError, setMismatchError] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const locked = wrongAttempts >= MAX_WRONG_ATTEMPTS;
  // Not disabled on an empty/untyped field: Chrome autofill writes the
  // input's DOM value without firing React's synthetic change event, so
  // `typed` can stay "" while the field visibly shows the right value.
  // Gating on `typed` would leave the button dead with zero feedback.
  // Validation happens at click time, read straight from the DOM.
  const confirmDisabled = locked || busy;

  // Reset typed/error state on close (lockout persists for component lifetime).
  function handleOpenChange(next: boolean) {
    if (busy) return;
    setOpen(next);
    if (!next) {
      setTyped("");
      setMismatchError(false);
    }
  }

  async function handleConfirm(event: React.MouseEvent<HTMLButtonElement>) {
    // Always prevent the default Radix close-on-action; we manage open state
    // manually so async work can finish before the dialog disappears.
    event.preventDefault();

    if (locked || busy) return;

    // Read from the DOM, not React state -- this is the one value autofill
    // can't lie to us about.
    const domValue = inputRef.current?.value ?? "";
    if (domValue !== typed) setTyped(domValue);

    if (domValue !== confirmText) {
      setWrongAttempts((n) => n + 1);
      setMismatchError(true);
      return;
    }

    setMismatchError(false);
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
            <span className="font-mono font-semibold text-foreground">
              {confirmText}
            </span>{" "}
            below to confirm.
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
          <div className="space-y-2">
            <Input
              ref={inputRef}
              value={typed}
              onChange={(e) => {
                setTyped(e.target.value);
                setMismatchError(false);
              }}
              placeholder={confirmText}
              autoComplete="off"
              name="confirm-phrase"
              spellCheck={false}
              disabled={busy}
              aria-label={`Type ${confirmText} to confirm`}
            />
            {mismatchError ? (
              <p role="alert" className="text-sm text-err">
                Type the exact id to confirm — autofill doesn&apos;t count.
              </p>
            ) : null}
          </div>
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
                "bg-destructive text-destructive-foreground hover:bg-destructive/90",
            )}
          >
            {busy ? "Working\u2026" : actionLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
