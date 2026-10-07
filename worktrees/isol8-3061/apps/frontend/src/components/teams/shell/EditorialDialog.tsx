"use client";

import type { ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export interface EditorialDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  /** Optional body. Confirm-only dialogs can carry their message in
   * `description` and omit a body entirely. */
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
}

export function EditorialDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  className,
}: EditorialDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(
          // Bounded height + a scrolling BODY (below), so a long form can never
          // push the footer past the fold: the dialog is centre-anchored and
          // `fixed`, so an overflowing one has nothing to scroll it back into
          // view and its Cancel/Save become unclickable (7 routine-variable rows
          // at 1280x720 did exactly that). `flex` overrides the base `grid` via
          // tailwind-merge, which keeps the footer content-sized at any row count.
          "flex max-h-[85svh] flex-col overflow-hidden border-[var(--edge)] bg-[var(--cream)] text-[var(--midnight)] sm:max-w-[520px]",
          className,
        )}
      >
        <DialogHeader>
          <DialogTitle className="font-editorial-serif text-title leading-tight text-[var(--midnight)]">
            {title}
          </DialogTitle>
          {description ? (
            <DialogDescription className="text-ui text-[var(--midnight-muted)]">
              {description}
            </DialogDescription>
          ) : (
            <DialogDescription className="sr-only">
              {/* Empty fallback to satisfy Radix a11y; consumer didn't supply one. */}
            </DialogDescription>
          )}
        </DialogHeader>
        {children != null && (
          <div data-dialog-body className="min-h-0 flex-1 space-y-3 overflow-y-auto py-2">
            {children}
          </div>
        )}
        {footer && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-[var(--edge)] pt-3">
            {footer}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
