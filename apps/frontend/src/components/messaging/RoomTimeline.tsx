"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { buildTimeline } from "@lightbulb/shared";
import type { RoomMessage } from "./types";
import "./messaging.css";

interface RoomTimelineProps {
  roomId: string;
  messages: readonly RoomMessage[];
  readSeq: number | null;
  ownLastSentSeq: number | null;
  /** Changes after a successful send, including while the optimistic row is still pending. */
  followToken?: number;
  /** Primitive revision for live-run and optimistic content rendered as children. */
  contentVersion?: string | number;
  targetMessageId?: string | null;
  /** Bump this (e.g. a monotonically increasing counter) to force a re-scroll/
   *  re-highlight of the same targetMessageId — otherwise re-selecting the same
   *  search result a second time is a no-op (see the `handled` ref below). */
  targetToken?: number;
  renderMessage: (message: RoomMessage, collapsed: boolean) => ReactNode;
  children?: ReactNode;
  empty: ReactNode;
  /** Sits above the oldest message once history is complete — the channel intro. */
  leading?: ReactNode;
}

const BOTTOM_THRESHOLD_PX = 48;
const HIGHLIGHT_MS = 2000;

const escapeSelector = (value: string) => typeof CSS !== "undefined" && "escape" in CSS ? CSS.escape(value) : value.replace(/["\\]/g, "\\$&");

interface LayoutSnapshot {
  roomId: string;
  oldestSeq: number | null;
  newestSeq: number;
  scrollHeight: number;
  clientHeight: number;
  followToken: number | undefined;
}

export function RoomTimeline({ roomId, messages, readSeq, ownLastSentSeq, followToken, contentVersion, targetMessageId, targetToken, renderMessage, children, empty, leading }: RoomTimelineProps): ReactElement {
  const region = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const smoothScrollInProgress = useRef(false);
  const layout = useRef<LayoutSnapshot | null>(null);
  const [unseen, setUnseen] = useState(0);
  const [hasNewContent, setHasNewContent] = useState(false);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const handled = useRef<string | null>(null);
  const items = useMemo(() => buildTimeline(messages, { readSeq }), [messages, readSeq]);
  const newest = messages.reduce((max, message) => Math.max(max, message.messageSeq), 0);
  const oldest = messages.length > 0 ? messages.reduce((min, message) => Math.min(min, message.messageSeq), Number.POSITIVE_INFINITY) : null;

  const scrollToBottom = useCallback((smooth = false) => {
    const node = region.current;
    if (!node) return;
    const reduceMotion = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const behavior: ScrollBehavior = smooth && !reduceMotion ? "smooth" : "auto";
    smoothScrollInProgress.current = behavior === "smooth";
    if (typeof node.scrollTo === "function") node.scrollTo({ top: node.scrollHeight, behavior });
    else node.scrollTop = node.scrollHeight;
    // Do not wait for the browser's asynchronous scroll event: streaming can
    // commit another chunk first and would otherwise detach follow mode again.
    // Clear the signal synchronously too: the programmatic scroll event may not
    // arrive before paint (or at all in a test/non-browser scrollport).
    atBottom.current = true;
    setUnseen(0);
    setHasNewContent(false);
  }, []);

  const markNewContent = useCallback((addedMessages = 0) => {
    if (addedMessages > 0) setUnseen((count) => count + addedMessages);
    setHasNewContent(true);
  }, []);

  // Message arrivals, history prepends, room changes, optimistic sends, and
  // synchronous live-run growth are all resolved before paint.
  useLayoutEffect(() => {
    const node = region.current;
    if (!node) return;
    const previous = layout.current;
    const roomChanged = !previous || previous.roomId !== roomId;
    const sent = !!previous && followToken !== previous.followToken;
    const prepended = !!previous && oldest !== null && previous.oldestSeq !== null
      && oldest < previous.oldestSeq && newest <= previous.newestSeq;

    if (roomChanged) {
      // Pre-paint reset prevents a stale pill from flashing across room changes.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      scrollToBottom();
    } else if (sent) {
      // Successful sends must clear detached state in the same commit.
      scrollToBottom();
    } else if (prepended) {
      // Compensate for inserted history so the same message stays under the
      // reader's eyes. This also prevents ResizeObserver from treating older
      // history as unread content.
      node.scrollTop += node.scrollHeight - previous.scrollHeight;
    } else if (newest > previous.newestSeq) {
      const added = messages.filter((message) => message.messageSeq > previous.newestSeq).length;
      if (atBottom.current || (ownLastSentSeq !== null && ownLastSentSeq > previous.newestSeq)) {
        // Keep caught-up state and its signal synchronized before paint.
        scrollToBottom();
      } else {
        markNewContent(added);
      }
    } else if (node.scrollHeight > previous.scrollHeight) {
      // Streaming text/tool blocks can grow without adding a RoomMessage.
      if (atBottom.current) {
        // Geometry and detached-state signals must update atomically.
        scrollToBottom();
      } else markNewContent();
    }

    layout.current = {
      roomId,
      oldestSeq: oldest,
      newestSeq: newest,
      scrollHeight: node.scrollHeight,
      clientHeight: node.clientHeight,
      followToken,
    };
  }, [contentVersion, followToken, markNewContent, messages, newest, oldest, ownLastSentSeq, roomId, scrollToBottom]);

  // Images, attachments, expanded tool output, and composer/viewport resizing
  // can change geometry without a React render. Observe both the scrollport
  // and its content so follow mode tracks those changes without polling.
  useEffect(() => {
    const node = region.current;
    const body = content.current;
    if (!node || !body || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const previous = layout.current;
      if (!previous || previous.roomId !== roomId) return;
      const grew = node.scrollHeight > previous.scrollHeight;
      const viewportChanged = node.clientHeight !== previous.clientHeight;
      if (atBottom.current && (grew || viewportChanged)) scrollToBottom();
      else if (grew) markNewContent();
      layout.current = {
        ...previous,
        scrollHeight: node.scrollHeight,
        clientHeight: node.clientHeight,
      };
    });
    observer.observe(node);
    observer.observe(body);
    return () => observer.disconnect();
  }, [markNewContent, roomId, scrollToBottom]);

  useEffect(() => {
    if (!targetMessageId) return;
    const key = `${targetMessageId}:${targetToken ?? 0}`;
    if (handled.current === key) return;
    const node = region.current?.querySelector<HTMLElement>(`[data-message-id="${escapeSelector(targetMessageId)}"]`);
    if (!node) return;
    handled.current = key;
    node.scrollIntoView({ block: "center" });
    node.focus({ preventScroll: true });
    // The highlight is deliberately coupled to this imperative focus effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHighlighted(targetMessageId);
    const timer = window.setTimeout(() => setHighlighted(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [targetMessageId, targetToken, messages.length]);

  function cancelSmoothFollow() {
    smoothScrollInProgress.current = false;
  }

  function onScroll() {
    const node = region.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_THRESHOLD_PX;
    if (layout.current) {
      layout.current = { ...layout.current, scrollHeight: node.scrollHeight, clientHeight: node.clientHeight };
    }
    // Intermediate events from a smooth "Jump to latest" animation are not an
    // intentional scroll-away. Wheel/touch/keyboard input cancels this guard.
    if (smoothScrollInProgress.current && !nearBottom) return;
    smoothScrollInProgress.current = false;
    atBottom.current = nearBottom;
    if (nearBottom) {
      setUnseen(0);
      setHasNewContent(false);
    }
  }

  const showJump = unseen > 0 || hasNewContent;
  const jumpLabel = unseen > 0
    ? `Jump to latest, ${unseen} new ${unseen === 1 ? "message" : "messages"}`
    : "Jump to latest";

  return <div className="room-timeline">
    <div ref={region} role="region" aria-label="Messages" className="room-history" tabIndex={0} onScroll={onScroll}
      onWheel={cancelSmoothFollow} onTouchStart={cancelSmoothFollow}
      onKeyDown={(event) => { if (["ArrowUp", "PageUp", "Home"].includes(event.key)) cancelSmoothFollow(); }}>
      <div ref={content} className="room-history-content">
        {leading}
        {items.length === 0 ? empty : items.map((item) => {
          if (item.kind === "date") return <div key={item.key} role="separator" aria-label={item.label} className="room-date-divider"><span>{item.label}</span></div>;
          if (item.kind === "new") return <div key={item.key} role="separator" aria-label="New messages" className="room-new-line"><span>New</span></div>;
          return <div key={item.key} data-message-id={item.message.messageId} data-highlight={highlighted === item.message.messageId ? "true" : undefined} tabIndex={-1} className="room-timeline-item">
            {renderMessage(item.message, item.collapsed)}
          </div>;
        })}
        {children}
      </div>
    </div>
    <span className="sr-only" role="status" aria-live="polite">
      {showJump ? (unseen > 0 ? `${unseen} new ${unseen === 1 ? "message" : "messages"}` : "New content available") : ""}
    </span>
    {showJump && <Button type="button" variant="outline" className="room-new-pill" onClick={() => scrollToBottom(true)} aria-label={jumpLabel}>
      <ArrowDown size={14} aria-hidden="true" />{unseen > 0 ? `${unseen} new ${unseen === 1 ? "message" : "messages"}` : "Jump to latest"}
    </Button>}
  </div>;
}
