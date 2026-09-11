"use client";

import { useEffect, useLayoutEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { buildTimeline } from "./timeline";
import type { RoomMessage } from "./types";
import "./messaging.css";

export interface RoomTimelineProps {
  roomId: string;
  messages: readonly RoomMessage[];
  readSeq: number | null;
  ownLastSentSeq: number | null;
  targetMessageId?: string | null;
  renderMessage: (message: RoomMessage, collapsed: boolean) => ReactNode;
  children?: ReactNode;
  empty: ReactNode;
}

const BOTTOM_THRESHOLD_PX = 48;
const HIGHLIGHT_MS = 2000;

const escapeSelector = (value: string) => typeof CSS !== "undefined" && "escape" in CSS ? CSS.escape(value) : value.replace(/["\\]/g, "\\$&");

export function RoomTimeline({ roomId, messages, readSeq, ownLastSentSeq, targetMessageId, renderMessage, children, empty }: RoomTimelineProps): ReactElement {
  const region = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const lastSeq = useRef<number>(messages.at(-1)?.messageSeq ?? 0);
  const handled = useRef<string | null>(null);
  const [unseen, setUnseen] = useState(0);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const items = buildTimeline(messages, { readSeq });

  function scrollToBottom() {
    const node = region.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
    atBottom.current = true;
    setUnseen(0);
  }

  // Room change: newest message on open.
  useLayoutEffect(() => {
    scrollToBottom();
    lastSeq.current = messages.at(-1)?.messageSeq ?? 0;
    handled.current = null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomId]);

  // Growth: stick to the bottom, or count unseen and show the pill.
  useLayoutEffect(() => {
    const newest = messages.at(-1)?.messageSeq ?? 0;
    if (newest <= lastSeq.current) return;
    const added = messages.filter((message) => message.messageSeq > lastSeq.current).length;
    lastSeq.current = newest;
    if (atBottom.current || (ownLastSentSeq !== null && newest === ownLastSentSeq)) scrollToBottom();
    else setUnseen((current) => current + added);
  }, [messages, ownLastSentSeq]);

  // Target message: scroll/focus/highlight once per target, so an unrelated
  // message arriving later doesn't re-trigger the same target. Kept in the
  // deps only so a target that isn't rendered yet (history still loading) is
  // retried as more messages come in.
  useEffect(() => {
    if (!targetMessageId || handled.current === targetMessageId) return;
    const node = region.current?.querySelector<HTMLElement>(`[data-message-id="${escapeSelector(targetMessageId)}"]`);
    if (!node) return;
    node.scrollIntoView({ block: "center" });
    node.focus({ preventScroll: true });
    handled.current = targetMessageId;
    setHighlighted(targetMessageId);
    const timer = setTimeout(() => setHighlighted(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [targetMessageId, messages.length]);

  function onScroll() {
    const node = region.current;
    if (!node) return;
    atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_THRESHOLD_PX;
    if (atBottom.current) setUnseen(0);
  }

  return <div className="room-timeline">
    <div ref={region} role="region" aria-label="Messages" className="room-history" tabIndex={0} onScroll={onScroll}>
      {items.length === 0 ? empty : items.map((item) => {
        if (item.kind === "date") return <div key={item.key} role="separator" aria-label={item.label} className="room-date-divider"><span>{item.label}</span></div>;
        if (item.kind === "new") return <div key={item.key} role="separator" aria-label="New messages" className="room-new-line"><span>New</span></div>;
        return <div key={item.key} data-message-id={item.message.messageId} data-highlight={highlighted === item.message.messageId ? "true" : undefined} tabIndex={-1} className="room-timeline-item">
          {renderMessage(item.message, item.collapsed)}
        </div>;
      })}
      {children}
    </div>
    {unseen > 0 && <Button type="button" variant="outline" className="room-new-pill" onClick={scrollToBottom} aria-label={`${unseen} new ${unseen === 1 ? "message" : "messages"}`}>
      <ArrowDown size={14} aria-hidden="true" />{unseen} new {unseen === 1 ? "message" : "messages"}
    </Button>}
  </div>;
}
