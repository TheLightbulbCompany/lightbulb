"use client";

// Live frames of an agent's browser tab, for everyone who does not hold the live view: AWS
// streams the live view to ONE screen, so the rest see the run's own tab as JPEG frames the
// bridge publishes to the room (`room.browser.frame`, ~5 a second while the page changes)
// and, when `interactive`, click, scroll and type into it through `POST /browser/input`.
// Mounted, it asks for frames (`/browser/viewing {frames: true}`); unmounted, casting stops
// on its own ~45 s later.

import { useCallback, useEffect, useRef, useState } from "react";
import type { RoomBrowserFrame } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export const FRAMES_HEARTBEAT_MS = 20_000;
/** Typed characters and wheel ticks are batched this long into one request. */
export const INPUT_BATCH_MS = 150;
/** Keys sent as key presses; every other printable key is typed as text. */
const KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

/** The latest frame of `runId` (any run's when absent) in the rooms this page follows. */
export function useBrowserFrame(runId?: string): RoomBrowserFrame | null {
  const { onEvent } = useLiveEvents();
  const [frame, setFrame] = useState<RoomBrowserFrame | null>(null);
  useEffect(() => onEvent((event) => {
    if (event.type !== "room.browser.frame" || (runId && event.payload.runId !== runId)) return;
    const next = event.payload;
    // AppSync keeps no order: never step back to an older frame of the same run.
    setFrame((current) => (current?.runId === next.runId && current.seq >= next.seq ? current : next));
  }), [onEvent, runId]);
  return frame;
}

export function BrowserFrameView({ runId, interactive = false, onTakeControl, empty, heartbeat = "/browser/viewing" }: {
  runId?: string;
  /** Where the "someone is watching frames" beat goes: the cloud browser's viewing heartbeat
   * by default, `/browser/frames` for a Mac tab (which must not start a cloud browser). */
  heartbeat?: string;
  interactive?: boolean;
  /** Shown until a frame arrives (none do while no agent browses). */
  empty?: React.ReactNode;
  /** Offered when the live view's one slot may be free: mounts the live view here. */
  onTakeControl?: () => void;
}): React.ReactElement {
  const api = useApi();
  const frame = useBrowserFrame(runId);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<{ text: string; deltaY: number; x: number; y: number; timer: ReturnType<typeof setTimeout> | null }>(
    { text: "", deltaY: 0, x: 0, y: 0, timer: null },
  );
  const frameRef = useRef(frame);
  frameRef.current = frame;

  useEffect(() => {
    const beat = () => void api.post(heartbeat, { frames: true }).catch(() => undefined);
    beat();
    const timer = setInterval(beat, FRAMES_HEARTBEAT_MS);
    return () => clearInterval(timer);
  }, [api, heartbeat]);

  const send = useCallback((event: Record<string, unknown>) => {
    const current = frameRef.current;
    if (!current) return;
    void (api.post("/browser/input", { runId: current.runId, ...event }) as Promise<unknown>).then(
      () => setError(null),
      (reason: unknown) => setError(reason instanceof Error ? reason.message : "That didn't reach the page."),
    );
  }, [api]);

  const flush = useCallback(() => {
    const batch = pending.current;
    if (batch.timer) clearTimeout(batch.timer);
    batch.timer = null;
    if (batch.text) send({ type: "text", text: batch.text });
    if (batch.deltaY) send({ type: "wheel", x: batch.x, y: batch.y, deltaY: Math.max(-5000, Math.min(5000, batch.deltaY)) });
    batch.text = "";
    batch.deltaY = 0;
  }, [send]);
  const later = () => {
    pending.current.timer ??= setTimeout(flush, INPUT_BATCH_MS);
  };
  useEffect(() => flush, [flush]);

  /** A pointer position on the picture, in the tab's own CSS pixels. The picture is scaled to
   * fit (object-contain), so the letterbox around it is subtracted first. */
  const at = (event: React.MouseEvent<HTMLImageElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const size = frameRef.current ?? { w: 0, h: 0 };
    const scale = Math.min(box.width / (size.w || 1), box.height / (size.h || 1)) || 1;
    const left = box.left + (box.width - size.w * scale) / 2;
    const top = box.top + (box.height - size.h * scale) / 2;
    const clamp = (value: number, max: number) => Math.max(0, Math.min(max, Math.round(value)));
    return { x: clamp((event.clientX - left) / scale, size.w), y: clamp((event.clientY - top) / scale, size.h) };
  };

  const input = interactive && frame ? {
    tabIndex: 0,
    onClick: (event: React.MouseEvent<HTMLImageElement>) => {
      event.currentTarget.focus();
      flush();
      send({ type: "click", ...at(event), button: event.button === 1 ? "middle" : "left" });
    },
    onWheel: (event: React.WheelEvent<HTMLImageElement>) => {
      Object.assign(pending.current, at(event));
      pending.current.deltaY += event.deltaY;
      later();
    },
    onKeyDown: (event: React.KeyboardEvent<HTMLImageElement>) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return; // the viewer's own shortcuts
      if (KEYS.has(event.key)) {
        flush();
        send({ type: "key", key: event.key });
      } else if (event.key.length === 1) {
        pending.current.text += event.key;
        later();
      } else {
        return;
      }
      event.preventDefault();
    },
    onPaste: (event: React.ClipboardEvent<HTMLImageElement>) => {
      const text = event.clipboardData.getData("text/plain").slice(0, 1000);
      if (!text) return;
      event.preventDefault();
      flush();
      send({ type: "text", text });
    },
  } : {};

  return (
    <div className="absolute inset-0 flex items-center justify-center overflow-hidden bg-[var(--cream)]" data-testid="browser-frame-view">
      {frame ? (
        // eslint-disable-next-line @next/next/no-img-element -- a live data: URL, never optimisable
        <img
          src={`data:image/jpeg;base64,${frame.jpegBase64}`}
          alt="The agent's browser tab, live"
          draggable={false}
          className={`h-full w-full select-none object-contain ${interactive ? "cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-[var(--bulb)]" : ""}`}
          {...input}
        />
      ) : (
        empty ?? <p role="status" className="text-sm text-[var(--midnight-muted)]">Waiting for the agent&apos;s page…</p>
      )}
      {(onTakeControl || error) && (
        <div className="absolute bottom-2 left-2 right-2 flex items-center justify-end gap-2">
          {error && <p role="alert" className="mr-auto rounded-md bg-[var(--midnight)] px-2 py-1 text-xs text-[var(--cream)]">{error}</p>}
          {onTakeControl && (
            <button type="button" onClick={onTakeControl}
              className="rounded-md bg-[var(--midnight)] px-3 py-1.5 text-xs font-medium text-[var(--cream)]">
              Take live control
            </button>
          )}
        </div>
      )}
    </div>
  );
}
