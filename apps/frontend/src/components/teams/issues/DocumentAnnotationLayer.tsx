"use client";

// Ported/adapted from upstream Paperclip `ui/src/components/DocumentAnnotationLayer.tsx`
// (A3). Draws highlight overlays for annotated spans and a floating "Comment"
// toolbar when the reader selects text inside the rendered document body.
// Self-contained: overlay rects only (no native CSS Highlight API), positions
// computed from Range.getClientRects().

import { useCallback, useEffect, useRef, useState } from "react";
import { MessageSquarePlus } from "lucide-react";
import {
  buildAnchorFromSelection,
  rangesForText,
} from "./lib/documentAnnotationSelection";
import type {
  AnnotationAnchorState,
  AnnotationThreadStatus,
  PendingAnchor,
} from "./lib/documentReviewTypes";

export interface OverlayThread {
  id: string;
  selectedText: string;
  status: AnnotationThreadStatus;
  anchorState: AnnotationAnchorState;
  // Stored normalized offset from the thread payload. Disambiguates which
  // occurrence to highlight when `selectedText` repeats in the document body.
  normalizedStart?: number | null;
}

interface HighlightRect {
  threadId: string;
  focused: boolean;
  resolved: boolean;
  top: number;
  left: number;
  width: number;
  height: number;
}

export function DocumentAnnotationLayer({
  containerRef,
  markdown,
  threads,
  focusedThreadId,
  onThreadFocus,
  onRequestComment,
  newCommentDisabled = false,
  newCommentDisabledReason,
}: {
  containerRef: React.RefObject<HTMLElement | null>;
  markdown: string;
  threads: OverlayThread[];
  focusedThreadId: string | null;
  onThreadFocus: (threadId: string) => void;
  onRequestComment: (anchor: PendingAnchor) => void;
  newCommentDisabled?: boolean;
  newCommentDisabledReason?: string | null;
}) {
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const [rects, setRects] = useState<HighlightRect[]>([]);
  const [pending, setPending] = useState<PendingAnchor | null>(null);
  const [toolbar, setToolbar] = useState<{ top: number; left: number } | null>(null);

  const recompute = useCallback(() => {
    const container = containerRef.current;
    const overlay = overlayRef.current;
    if (!container || !overlay) {
      setRects([]);
      return;
    }
    const overlayRect = overlay.getBoundingClientRect();
    const next: HighlightRect[] = [];
    for (const thread of threads) {
      if (thread.anchorState === "orphaned") continue;
      const resolved = thread.status === "resolved";
      if (resolved && thread.id !== focusedThreadId) continue;
      for (const range of rangesForText(container, thread.selectedText, thread.normalizedStart)) {
        for (const rect of Array.from(range.getClientRects())) {
          if (rect.width === 0 || rect.height === 0) continue;
          next.push({
            threadId: thread.id,
            focused: thread.id === focusedThreadId,
            resolved,
            top: rect.top - overlayRect.top,
            left: rect.left - overlayRect.left,
            width: rect.width,
            height: rect.height,
          });
        }
      }
    }
    setRects(next);
  }, [containerRef, focusedThreadId, threads]);

  // Recompute overlay rects out-of-band (rAF-scheduled) so we never call
  // setState synchronously inside the effect body, and re-run on resize/scroll
  // + when the rendered body mutates. Mirrors the reference layer's scheduling.
  useEffect(() => {
    if (typeof window === "undefined") return;
    let cancelled = false;
    let frame: number | null = null;
    const schedule = () => {
      if (cancelled || frame !== null) return;
      frame = window.requestAnimationFrame(() => {
        frame = null;
        if (!cancelled) recompute();
      });
    };
    schedule();
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    const container = containerRef.current;
    const resizeObserver =
      typeof window.ResizeObserver === "function" ? new window.ResizeObserver(schedule) : null;
    if (resizeObserver && container) resizeObserver.observe(container);
    const mutationObserver =
      typeof window.MutationObserver === "function" && container
        ? new window.MutationObserver(schedule)
        : null;
    if (mutationObserver && container) {
      mutationObserver.observe(container, { childList: true, characterData: true, subtree: true });
    }
    return () => {
      cancelled = true;
      if (frame !== null) window.cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true);
      resizeObserver?.disconnect();
      mutationObserver?.disconnect();
    };
  }, [recompute, containerRef]);

  // Capture a text selection inside the document body → pending anchor + toolbar.
  useEffect(() => {
    if (typeof document === "undefined") return;
    const onSelection = () => {
      const container = containerRef.current;
      const overlay = overlayRef.current;
      if (!container || !overlay) return;
      const selection = window.getSelection();
      if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
        setPending(null);
        setToolbar(null);
        return;
      }
      const range = selection.getRangeAt(0);
      if (!container.contains(range.commonAncestorContainer)) {
        setPending(null);
        setToolbar(null);
        return;
      }
      const anchor = buildAnchorFromSelection({ markdown, container, range });
      if (!anchor) {
        setPending(null);
        setToolbar(null);
        return;
      }
      const overlayRect = overlay.getBoundingClientRect();
      const rect = range.getBoundingClientRect();
      setPending(anchor);
      setToolbar({
        top: Math.max(0, rect.top - overlayRect.top - 34),
        left: Math.max(0, rect.left - overlayRect.left + rect.width / 2 - 48),
      });
    };
    document.addEventListener("selectionchange", onSelection);
    return () => document.removeEventListener("selectionchange", onSelection);
  }, [containerRef, markdown]);

  return (
    <div
      ref={overlayRef}
      className="pointer-events-none absolute inset-0"
      aria-hidden={rects.length === 0 && !pending}
      data-testid="document-annotation-layer"
    >
      {rects.map((rect, index) => (
        <button
          key={`${rect.threadId}-${index}`}
          type="button"
          data-thread-id={rect.threadId}
          aria-label="Open annotation thread"
          className="pointer-events-auto absolute cursor-pointer rounded-sm transition-colors"
          style={{
            top: rect.top,
            left: rect.left,
            width: rect.width,
            height: rect.height,
            background: rect.focused
              ? "color-mix(in srgb, var(--midnight) 38%, transparent)"
              : rect.resolved
                ? "color-mix(in srgb, var(--midnight-muted) 22%, transparent)"
                : "color-mix(in srgb, var(--midnight) 20%, transparent)",
          }}
          onMouseDown={(event) => {
            event.preventDefault();
            onThreadFocus(rect.threadId);
          }}
        />
      ))}
      {pending && toolbar ? (
        <div
          role="toolbar"
          aria-label="Selection actions"
          data-testid="document-annotation-selection-toolbar"
          className="pointer-events-auto absolute z-10 flex items-center rounded-md px-1 py-1 shadow-md"
          style={{
            top: toolbar.top,
            left: toolbar.left,
            background: "var(--surface, var(--panel))",
            border: "1px solid var(--edge)",
          }}
          onMouseDown={(event) => event.preventDefault()}
        >
          <button
            type="button"
            className="flex h-7 items-center gap-1 rounded-sm px-2 text-caption disabled:opacity-50"
            style={{ color: "var(--midnight)" }}
            disabled={newCommentDisabled}
            title={newCommentDisabled ? newCommentDisabledReason ?? undefined : "Comment on selection"}
            onClick={() => {
              if (pending) onRequestComment(pending);
              setPending(null);
              setToolbar(null);
            }}
          >
            <MessageSquarePlus size={16} aria-hidden="true" />
            Comment
          </button>
        </div>
      ) : null}
    </div>
  );
}
