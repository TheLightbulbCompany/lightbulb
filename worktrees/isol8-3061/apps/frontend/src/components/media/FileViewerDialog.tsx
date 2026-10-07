"use client";

import * as React from "react";
import { ChevronLeft, ChevronRight, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { formatBytes } from "@/lib/utils";
import { FileViewerContent, type FileViewerItem } from "./FileViewerContent";

export interface FileViewerAttachment extends Omit<FileViewerItem, "url"> {
  /** Stable identity for the attachment (message attachment id, etc). */
  id: string;
  /** Resolved object/HTTP URL for the bytes — undefined while still downloading. */
  url?: string;
}

export interface FileViewerDialogProps {
  attachments: readonly FileViewerAttachment[];
  /** Index into `attachments` currently shown; out of range (e.g. -1) keeps the dialog closed. */
  index: number;
  onIndexChange: (index: number) => void;
  onOpenChange: (open: boolean) => void;
  /**
   * The element to return focus to on close. Radix's Dialog only auto-restores
   * focus to a `Dialog.Trigger`, but this dialog is opened from whichever of
   * several thumbnail buttons the caller rendered, not one static Trigger —
   * the caller records the clicked button here (in its own click handler) so
   * onCloseAutoFocus can send focus back to the exact one that opened it.
   */
  restoreFocusRef?: React.RefObject<HTMLElement | null>;
}

/**
 * The overlay opened by clicking a chat attachment: a focus-trapped Radix
 * dialog (Escape / backdrop-click close and focus-return to the trigger all
 * come free from Dialog) with ←/→ paging across a message's attachments and a
 * download affordance that preserves the real filename. Rendering the actual
 * bytes is FileViewerContent's job — this component is only the chrome
 * (header, paging, close, download) around it.
 */
export function FileViewerDialog({ attachments, index, onIndexChange, onOpenChange, restoreFocusRef }: FileViewerDialogProps) {
  const open = index >= 0 && index < attachments.length;
  const item = open ? attachments[index] : null;
  const hasMultiple = attachments.length > 1;

  const goTo = React.useCallback(
    (next: number) => onIndexChange((next + attachments.length) % attachments.length),
    [attachments.length, onIndexChange],
  );

  function handleKeyDown(event: React.KeyboardEvent) {
    if (!hasMultiple) return;
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      goTo(index - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      goTo(index + 1);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onOpenChange(false); }}>
      {item && (
        <DialogContent
          showCloseButton
          onKeyDown={handleKeyDown}
          onCloseAutoFocus={(event) => {
            if (!restoreFocusRef?.current) return;
            event.preventDefault();
            restoreFocusRef.current.focus();
          }}
          className="flex max-h-[90svh] w-[min(94vw,1100px)] max-w-none flex-col gap-3 border-edge bg-cream p-4 text-midnight sm:rounded-lg"
        >
          <DialogHeader className="pr-6 text-left">
            <DialogTitle className="max-w-full truncate text-sm font-semibold">{item.fileName}</DialogTitle>
            <DialogDescription className="text-xs text-midnight-muted">
              {item.size != null ? formatBytes(item.size) : null}
              {hasMultiple ? `${item.size != null ? " · " : ""}${index + 1} of ${attachments.length}` : null}
            </DialogDescription>
          </DialogHeader>

          <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-auto rounded-md bg-cream-deep">
            {!item.url ? (
              <p role="status" className="text-sm text-midnight-muted">Loading…</p>
            ) : (
              <FileViewerContent item={{ ...item, url: item.url }} className="max-h-[70svh]" />
            )}

            {hasMultiple && (
              <>
                <Button
                  type="button"
                  variant="secondary"
                  size="icon"
                  aria-label="Previous attachment"
                  onClick={() => goTo(index - 1)}
                  className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-cream/90 shadow-md hover:bg-cream"
                >
                  <ChevronLeft />
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  size="icon"
                  aria-label="Next attachment"
                  onClick={() => goTo(index + 1)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-cream/90 shadow-md hover:bg-cream"
                >
                  <ChevronRight />
                </Button>
              </>
            )}
          </div>

          {item.url && (
            <div className="flex shrink-0 justify-end">
              <Button asChild variant="secondary" size="sm">
                <a href={item.url} download={item.fileName}>
                  <Download size={16} />
                  Download
                </a>
              </Button>
            </div>
          )}
        </DialogContent>
      )}
    </Dialog>
  );
}
