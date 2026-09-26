"use client";

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactElement, type ReactNode } from "react";
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
  historyComplete?: boolean;
  historyLoading?: boolean;
  historyError?: string | null;
  onLoadOlder?: () => void;
  /** Bump on every send: the viewer's own message always brings them to the bottom. */
  sendToken?: number;
}

// Slack-like "near the bottom": within this many px the reader keeps following.
const BOTTOM_THRESHOLD_PX = 80;
const HIGHLIGHT_MS = 2000;

const escapeSelector = (value: string) => typeof CSS !== "undefined" && "escape" in CSS ? CSS.escape(value) : value.replace(/["\\]/g, "\\$&");

const reducedMotion = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/**
 * Follow-the-bottom for a scroll region. `content` wraps everything inside it,
 * so ANY height change follows — a streaming run, a link preview or image that
 * loads after its message, the composer growing and shrinking the viewport.
 *
 * The reader stops following only by moving UP. A scroll event that merely
 * reports content having grown under a pinned reader (the pin landed, then a
 * streamed chunk bigger than the threshold arrived before the event fired) must
 * not count as "scrolled away" — that race is what left the reply below the fold.
 */
export function useStickToBottom() {
  const region = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const lastTop = useRef(0);
  const pin = useCallback((smooth = false) => {
    const node = region.current;
    if (!node) return;
    atBottom.current = true;
    if (smooth && typeof node.scrollTo === "function" && !reducedMotion()) node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
    else node.scrollTop = node.scrollHeight;
  }, []);
  useEffect(() => {
    const node = region.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => { if (atBottom.current) node.scrollTop = node.scrollHeight; });
    observer.observe(node);
    if (content.current) observer.observe(content.current);
    return () => observer.disconnect();
  }, []);
  /** Returns whether the reader is (still) following the bottom. */
  const onScroll = useCallback(() => {
    const node = region.current;
    if (!node) return atBottom.current;
    const top = node.scrollTop;
    if (node.scrollHeight - top - node.clientHeight <= BOTTOM_THRESHOLD_PX) atBottom.current = true;
    else if (top < lastTop.current) atBottom.current = false;
    lastTop.current = top;
    return atBottom.current;
  }, []);
  return { region, content, atBottom, pin, onScroll };
}

/** A scroll box that follows its bottom (thread replies); bump `pinToken` to bring the reader down. */
export function StickToBottom({ className, pinToken = 0, children }: { className?: string; pinToken?: number; children: ReactNode }): ReactElement {
  const { region, content, pin, onScroll } = useStickToBottom();
  useLayoutEffect(() => { if (pinToken) pin(true); }, [pinToken, pin]);
  return <div ref={region} className={className} onScroll={onScroll}><div ref={content}>{children}</div></div>;
}

export function RoomTimeline({ roomId, messages, readSeq, ownLastSentSeq, targetMessageId, targetToken, renderMessage, children, empty, leading, historyComplete = true, historyLoading = false, historyError, onLoadOlder, sendToken = 0 }: RoomTimelineProps): ReactElement {
  const { region, content, atBottom, pin, onScroll: follow } = useStickToBottom();
  const lastSeq = useRef<number>(messages.at(-1)?.messageSeq ?? 0);
  const handled = useRef<string | null>(null);
  const [unseen, setUnseen] = useState(0);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const olderAnchor = useRef<{ id: string; top: number } | null>(null);
  const items = useMemo(() => buildTimeline(messages, { readSeq }), [messages, readSeq]);

  function scrollToBottom(smooth = false) {
    pin(smooth);
    setUnseen(0);
  }

  // Room change: newest message on open.
  useLayoutEffect(() => {
    scrollToBottom();
    lastSeq.current = messages.at(-1)?.messageSeq ?? 0;
    handled.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomId]);

  // Own send: always to the bottom, whatever the reader was looking at.
  useLayoutEffect(() => {
    if (sendToken) scrollToBottom(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sendToken]);

  // Growth: stick to the bottom, or count unseen and show the pill.
  useLayoutEffect(() => {
    const newest = messages.at(-1)?.messageSeq ?? 0;
    if (newest <= lastSeq.current) return;
    const added = messages.filter((message) => message.messageSeq > lastSeq.current).length;
    lastSeq.current = newest;
    if (atBottom.current || (ownLastSentSeq !== null && newest === ownLastSentSeq)) scrollToBottom();
    else setUnseen((current) => current + added);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, ownLastSentSeq]);

  useLayoutEffect(() => {
    const anchor = olderAnchor.current;
    const node = region.current;
    if (!anchor || !node || historyLoading) return;
    const row = node.querySelector<HTMLElement>(`[data-message-id="${escapeSelector(anchor.id)}"]`);
    if (row) node.scrollTop += row.getBoundingClientRect().top - anchor.top;
    olderAnchor.current = null;
  }, [messages, historyLoading, region]);

  // Target message: scroll/focus/highlight once per target, so an unrelated
  // message arriving later doesn't re-trigger the same target. Keyed on
  // targetToken too so re-selecting the same message (e.g. the same search
  // result twice) still re-scrolls. Kept in the deps only so a target that
  // isn't rendered yet (history still loading) is retried as more messages
  // come in.
  useEffect(() => {
    const key = `${targetToken ?? 0}:${targetMessageId}`;
    if (!targetMessageId || handled.current === key) return;
    const node = region.current?.querySelector<HTMLElement>(`[data-message-id="${escapeSelector(targetMessageId)}"]`);
    if (!node) return;
    node.scrollIntoView({ block: "center" });
    node.focus({ preventScroll: true });
    handled.current = key;
    setHighlighted(targetMessageId);
    const timer = setTimeout(() => setHighlighted(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [targetMessageId, targetToken, messages.length, region]);

  function onScroll() {
    if (follow()) setUnseen(0);
  }

  return <div className="room-timeline">
    <div ref={region} role="region" aria-label="Messages" className="room-history" tabIndex={0} onScroll={onScroll}>
      <div ref={content} className="room-history-content">
        {!historyComplete && onLoadOlder && <div className="room-older-history">
          <Button type="button" variant="outline" disabled={historyLoading} onClick={() => {
            const node = region.current;
            const row = node && [...node.querySelectorAll<HTMLElement>("[data-message-id]")].find((candidate) =>
              candidate.getBoundingClientRect().bottom > node.getBoundingClientRect().top);
            if (row) olderAnchor.current = { id: row.dataset.messageId!, top: row.getBoundingClientRect().top };
            onLoadOlder();
          }}>{historyLoading ? "Loading…" : "Load older messages"}</Button>
          {historyError && <p role="alert">Could not load older messages. Try again.</p>}
        </div>}
        {leading}
        {items.length === 0 ? empty : <TimelineMessages items={items} highlighted={highlighted} renderMessage={renderMessage} />}
        {children}
      </div>
    </div>
    {unseen > 0 && <Button type="button" variant="outline" className="room-new-pill" onClick={() => scrollToBottom(true)} aria-label={`${unseen} new ${unseen === 1 ? "message" : "messages"}`}>
      <ArrowDown size={14} aria-hidden="true" />{unseen} new {unseen === 1 ? "message" : "messages"}
    </Button>}
  </div>;
}

// Drafts and live runs change beside history. Keep those renders out of the
// canonical rows; normal shallow comparison still honors new action callbacks.
const TimelineMessages = memo(function TimelineMessages({ items, highlighted, renderMessage }: {
  items: ReturnType<typeof buildTimeline>;
  highlighted: string | null;
  renderMessage: RoomTimelineProps["renderMessage"];
}) {
  // Highlighting changes the wrapper, not the message body or its actions.
  const contents = useMemo(() => items.map((item) =>
    item.kind === "message" ? renderMessage(item.message, item.collapsed) : null
  ), [items, renderMessage]);
  return items.map((item, index) => {
    if (item.kind === "date") return <div key={item.key} role="separator" aria-label={item.label} className="room-date-divider"><span>{item.label}</span></div>;
    if (item.kind === "new") return <div key={item.key} role="separator" aria-label="New messages" className="room-new-line"><span>New</span></div>;
    return <div key={item.key} data-message-id={item.message.messageId} data-highlight={highlighted === item.message.messageId ? "true" : undefined} tabIndex={-1} className="room-timeline-item">
      {contents[index]}
    </div>;
  });
});
