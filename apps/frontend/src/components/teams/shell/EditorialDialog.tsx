"use client";

import type { ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export interface EditorialDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}

export function EditorialDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
}: EditorialDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="border-[var(--edge)] bg-[var(--cream)] text-[var(--ink)] sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="font-editorial-serif text-[22px] leading-tight text-[var(--ink)]">
            {title}
          </DialogTitle>
          {description && (
            <DialogDescription className="text-[13px] text-[var(--mute)]">
              {description}
            </DialogDescription>
          )}
        </DialogHeader>
        <div className="space-y-3 py-2">{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-[var(--edge)] pt-3">
            {footer}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
