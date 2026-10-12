"use client";

// Live frames of an agent's browser tab, for everyone who does not hold the live view: AWS
// streams the live view to ONE screen, so the rest see the run's own tab as JPEG frames the
// bridge publishes to the room (`room.browser.frame`, ~5 a second while the page changes)
// and, when `interactive`, click, scroll and type into it through `POST /browser/input`.
// Mounted, it asks for frames (`/browser/viewing {frames: true}`); unmounted, casting stops
// on its own ~45 s later.
//
// With `mac`, it is a paired Mac's tab instead: sharp view-tier frames on the room's frames
// sibling channel (claimed while mounted), and the viewer's pointer, wheel and keys go to
// `POST /browser/mac/input` through one in-flight queue, so nothing reorders.

import { useCallback, useEffect, useRef, useState } from "react";
import type { RoomBrowserFrame } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";

export const FRAMES_HEARTBEAT_MS = 20_000;
/** Typed characters and wheel ticks are batched this long into one request. */
export const INPUT_BATCH_MS = 150;
/** A view frame this much older than the newest thumb is frozen: show the thumb instead. */
const VIEW_STALE_MS = 2000;
/** Mac input: at most this many events per request (the backend's MAX_INPUT_EVENTS). */
export const MAX_INPUT_EVENTS = 32;
/** The most Mac input kept waiting behind a slow request. */
export const MAX_QUEUE = 4 * MAX_INPUT_EVENTS;
/** Longest text or paste event the backend takes; longer ones are split. */
const MAX_TEXT = 1000;
/** Keys sent as key presses; every other printable key is typed as text. */
const KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End", "PageUp", "PageDown"]);

/** The latest frame of `runId` (any run's when absent) in the rooms this page follows. The
 * newest view-tier frame and the newest other frame are kept apart: `prefer: "view"` shows the
 * view frame unless the other is over VIEW_STALE_MS newer (the view stream stopped); otherwise
 * the newer of the two. */
export function useBrowserFrame(runId?: string, prefer?: "view"): RoomBrowserFrame | null {
  const { onEvent } = useLiveEvents();
  // Frames are kept for the run they were caught for: another run starts blank, so its view
  // never shows (or maps clicks onto) the previous run's picture.
  type Held = { runId?: string; view: RoomBrowserFrame | null; other: RoomBrowserFrame | null };
  const [frames, setFrames] = useState<Held>({ runId, view: null, other: null });
  useEffect(() => onEvent((event) => {
    if (event.type !== "room.browser.frame" || (runId && event.payload.runId !== runId)) return;
    // The run-less cloud DCV fallback must never show a Mac cast frame or send
    // its click through the cloud input route. Mac frames always name a tier.
    if (!runId && event.payload.tier) return;
    const next = event.payload;
    const tier = next.tier === "view" ? "view" : "other";
    // AppSync keeps no order: never step back to an older frame of the same run and tier.
    setFrames((stored) => {
      const current = stored.runId === runId ? stored : { runId, view: null, other: null };
      const held = current[tier];
      return held?.runId === next.runId && held.seq >= next.seq ? current : { ...current, [tier]: next };
    });
  }), [onEvent, runId]);
  const { view, other } = frames.runId === runId ? frames : { view: null, other: null };
  // Both seqs are epoch ms.
  if (prefer === "view") return view && !(other && other.seq - view.seq > VIEW_STALE_MS) ? view : other;
  return !view || (other && other.seq >= view.seq) ? other : view;
}

/** The latest frame of a shared session's browser (`workspace.session.screen`, kind "image", on the
 * session's frames channel), in the room frame's shape; newest seq wins. */
export function useSessionBrowserFrame(sessionId?: string): RoomBrowserFrame | null {
  const { onEvent } = useLiveEvents();
  const [held, setHeld] = useState<{ sessionId?: string; frame: RoomBrowserFrame | null }>({ sessionId, frame: null });
  useEffect(() => {
    if (!sessionId) return undefined;
    return onEvent((event) => {
      if (event.type !== "workspace.session.screen" || event.payload.kind !== "image" || event.payload.sessionId !== sessionId) return;
      const { data, w, h, seq } = event.payload;
      setHeld((current) => {
        const kept = current.sessionId === sessionId ? current.frame : null;
        return kept && kept.seq >= seq ? current
          : { sessionId, frame: { roomId: "", agentId: "", runId: sessionId, seq, jpegBase64: data, w, h } };
      });
    });
  }, [onEvent, sessionId]);
  return held.sessionId === sessionId ? held.frame : null;
}

type MacEvent = { type: string; deltaY?: number } & Record<string, unknown>;

/** A Mac tab's input queue: one request in flight; whatever arrives meanwhile waits, with runs
 * of `move`s coalesced to the last and runs of `wheel`s summed, and goes ≤32 at a time, in order. */
function useMacInput(runId: string | undefined, deviceId: string | undefined, roomId: string | undefined,
  onResult: (error: string | null) => void, sessionId?: string) {
  const api = useApi();
  // The target lives beside the queue, so a request settling after a switch pumps the NEW run's
  // queue to the new run; `gen` fences the old request's rejection off the new queue.
  const state = useRef<{ queue: MacEvent[]; busy: boolean; runId?: string; deviceId?: string; roomId?: string; sessionId?: string; gen: number }>(
    { queue: [], busy: false, gen: 0 },
  );
  // Input queued for one run/Mac never reaches another.
  useEffect(() => {
    const current = state.current;
    current.queue.length = 0;
    current.runId = runId;
    current.deviceId = deviceId;
    current.roomId = roomId;
    current.sessionId = sessionId;
    current.gen += 1;
  }, [runId, deviceId, roomId, sessionId]);
  const pump = useCallback(() => {
    const next = () => {
      const current = state.current;
      const { runId: run, deviceId: device, roomId: room, sessionId: session, gen } = current;
      if (current.busy || !current.queue.length || (!session && (!run || !device))) return;
      const events = current.queue.splice(0, MAX_INPUT_EVENTS);
      current.busy = true;
      // A shared session's browser takes the same events on its own route (spec 2026-10-11).
      const sent = session
        ? api.post(`/messaging/sessions/${encodeURIComponent(session)}/browser/input`, { events })
        : api.post("/browser/mac/input", { runId: run, deviceId: device, ...(room && { roomId: room }), events });
      void (sent as Promise<unknown>).then(
        () => { if (current.gen === gen) onResult(null); },
        (reason: unknown) => {
          if (current.gen !== gen) return; // a refusal for a run nobody is watching any more
          // What queued behind a refused batch was aimed at a page that may not be there: drop it.
          current.queue.length = 0;
          onResult(reason instanceof Error ? reason.message : "That didn't reach the page.");
        },
      ).finally(() => {
        current.busy = false;
        next();
      });
    };
    next();
  }, [api, onResult]);
  return useCallback((event: MacEvent) => {
    const queue = state.current.queue;
    const last = queue[queue.length - 1];
    if (event.type === "move" && last?.type === "move") queue[queue.length - 1] = event;
    else if (event.type === "wheel" && last?.type === "wheel") {
      queue[queue.length - 1] = { ...event, deltaY: Math.max(-5000, Math.min(5000, (last.deltaY ?? 0) + (event.deltaY ?? 0))) };
    } else queue.push(event);
    // ponytail: a hung request lets input pile up; keep the newest MAX_QUEUE events and drop the
    // oldest (a dropped down can orphan its up). Smarter pruning if people hit it.
    if (queue.length > MAX_QUEUE) queue.splice(0, queue.length - MAX_QUEUE);
    // Events of the same task (a pointer down and up, typed text) leave in one batch.
    queueMicrotask(pump);
  }, [pump]);
}

/** CDP modifier bits: Alt=1, Ctrl=2, Meta=4, Shift=8. */
const modifiersOf = (event: React.KeyboardEvent) =>
  (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
const BUTTONS = ["left", "middle", "right"] as const;
/** Presses of one button this close in time and place count as a double (then triple) click. */
const CLICK_MS = 500;
const CLICK_SLOP_PX = 5;
/** `text` in pieces of at most MAX_TEXT characters (code points, so no pair is cut in two). */
function chunks(text: string): string[] {
  const chars = [...text];
  const out: string[] = [];
  for (let at = 0; at < chars.length; at += MAX_TEXT) out.push(chars.slice(at, at + MAX_TEXT).join(""));
  return out;
}

export function BrowserFrameView({
  runId, interactive = false, mac, session, prefer, onTakeControl, empty, heartbeat = "/browser/viewing", heartbeatBody,
}: {
  runId?: string;
  /** Where the "someone is watching frames" beat goes: the cloud browser's viewing heartbeat
   * by default, `/browser/frames` for a Mac tab (which must not start a cloud browser). */
  heartbeat?: string;
  /** The beat's body: `{frames: true}` by default. */
  heartbeatBody?: Record<string, unknown>;
  interactive?: boolean;
  /** A paired Mac's tab, live and interactive: view-tier frames, input to `/browser/mac/input`
   * (overrides `heartbeat` and `interactive`). */
  mac?: { deviceId: string; roomId?: string };
  prefer?: "view";
  /** Shown until a frame arrives (none do while no agent browses). */
  empty?: React.ReactNode;
  /** Offered when the live view's one slot may be free: mounts the live view here. */
  onTakeControl?: () => void;
  /** A shared Claude Code or Codex session's browser (spec 2026-10-11): frames from the session's
   * frames channel (the caller holds that claim), the beat to the session, and with `canType` the
   * Mac's input shape to the session's own input route; read-only without it. */
  session?: { sessionId: string; canType: boolean };
}): React.ReactElement {
  const api = useApi();
  const { claimFrames } = useLiveEvents();
  const roomFrame = useBrowserFrame(session ? undefined : runId, prefer);
  const sessionFrame = useSessionBrowserFrame(session?.sessionId);
  // A session never shows a room's frames.
  const frame = session ? sessionFrame : roomFrame;
  const [error, setError] = useState<string | null>(null);
  const isMac = !!mac;
  const macLike = isMac || !!session?.canType;
  const live = interactive || macLike;
  const macSend = useMacInput(runId, mac?.deviceId, mac?.roomId, setError, session?.sessionId);
  /** The button held on the Mac (`down` false once its up, or a cancel, went out) and the last
   * press, for counting double and triple clicks ourselves: pointer events carry no click count. */
  const held = useRef<{ button: (typeof BUTTONS)[number]; clicks: number; down: boolean; t: number; x: number; y: number }>(
    { button: "left", clicks: 1, down: false, t: 0, x: 0, y: 0 },
  );
  const pending = useRef<{ text: string; deltaY: number; x: number; y: number; timer: ReturnType<typeof setTimeout> | null }>(
    { text: "", deltaY: 0, x: 0, y: 0, timer: null },
  );
  const composing = useRef(false);
  const compositionEcho = useRef<string | null>(null);
  const macEditor = useRef<HTMLInputElement>(null);
  const frameRef = useRef(frame);
  frameRef.current = frame;
  // Typing or scrolling still batching for one run/Mac is discarded when the view moves to
  // another (React reuses this component), so its timer can never deliver it to the new one.
  const deviceId = mac?.deviceId;
  useEffect(() => () => {
    const batch = pending.current;
    if (batch.timer) clearTimeout(batch.timer);
    batch.timer = null;
    batch.text = "";
    batch.deltaY = 0;
    composing.current = false;
    compositionEcho.current = null;
    if (macEditor.current) macEditor.current.value = "";
  }, [runId, deviceId, mac?.roomId, session?.sessionId]);

  const beatPath = session ? `/messaging/sessions/${encodeURIComponent(session.sessionId)}/browser/viewing` : isMac ? "/browser/frames" : heartbeat;
  const beatBody = JSON.stringify(session ? {} : isMac ? { runId, deviceId, tier: "view", roomId: mac?.roomId } : heartbeatBody ?? { frames: true });
  useEffect(() => {
    if (!beatPath) return;
    const beat = () => void api.post(beatPath, JSON.parse(beatBody)).catch(() => undefined);
    beat();
    const timer = setInterval(beat, FRAMES_HEARTBEAT_MS);
    return () => clearInterval(timer);
  }, [api, beatPath, beatBody]);
  // The view-tier frames ride the room's frames sibling channel, subscribed while claimed.
  useEffect(() => (isMac ? claimFrames() : undefined), [isMac, claimFrames]);

  const send = useCallback((event: Record<string, unknown>) => {
    if (macLike) {
      macSend(event as MacEvent);
      return;
    }
    const current = frameRef.current;
    if (!current) return;
    void (api.post("/browser/input", { runId: current.runId, ...event }) as Promise<unknown>).then(
      () => setError(null),
      (reason: unknown) => setError(reason instanceof Error ? reason.message : "That didn't reach the page."),
    );
  }, [api, macLike, macSend]);

  const flush = useCallback(() => {
    const batch = pending.current;
    if (batch.timer) clearTimeout(batch.timer);
    batch.timer = null;
    if (batch.text && macLike) chunks(batch.text).forEach((text) => send({ type: "text", text }));
    else if (batch.text) send({ type: "text", text: batch.text });
    if (batch.deltaY) send({ type: "wheel", x: batch.x, y: batch.y, deltaY: Math.max(-5000, Math.min(5000, batch.deltaY)) });
    batch.text = "";
    batch.deltaY = 0;
  }, [send, macLike]);
  const later = () => {
    pending.current.timer ??= setTimeout(flush, INPUT_BATCH_MS);
  };
  useEffect(() => flush, [flush]);

  const macKeyDown = (event: React.KeyboardEvent<HTMLElement>, editable = false) => {
    if (editable && (composing.current || event.nativeEvent.isComposing)) return;
    const { key } = event;
    const command = (event.metaKey || event.ctrlKey) && /^[azy]$/i.test(key);
    if (KEYS.has(key) || command) {
      flush();
      send({ type: "key", key, modifiers: modifiersOf(event) });
    } else if (event.metaKey || event.ctrlKey || event.altKey || editable) {
      return; // shortcuts stay local; editable text arrives through input/composition
    } else if ([...key].length === 1) {
      pending.current.text += key;
      later();
    } else {
      return;
    }
    event.preventDefault();
  };
  const macPaste = (event: React.ClipboardEvent<HTMLElement>) => {
    const text = event.clipboardData.getData("text/plain");
    if (!text) return;
    event.preventDefault();
    flush();
    chunks(text).forEach((piece) => send({ type: "paste", text: piece }));
  };
  const macInputKey = (inputType: string) => {
    const key = inputType === "deleteContentBackward" ? "Backspace" : inputType === "insertLineBreak" ? "Enter" : null;
    if (!key) return false;
    flush();
    send({ type: "key", key, modifiers: 0 });
    return true;
  };
  const commitMacText = (target: HTMLInputElement) => {
    if (!target.value) return;
    pending.current.text += target.value;
    target.value = "";
    later();
  };
  const macTextInput = (event: React.FormEvent<HTMLInputElement>) => {
    if (composing.current || (event.nativeEvent as InputEvent).isComposing) return;
    const target = event.currentTarget;
    if (compositionEcho.current && target.value === compositionEcho.current) {
      target.value = ""; // the input event that echoes compositionend, not a second commit
      compositionEcho.current = null;
      return;
    }
    compositionEcho.current = null;
    if (macInputKey((event.nativeEvent as InputEvent).inputType)) {
      target.value = "";
      return;
    }
    commitMacText(target);
  };

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

  const onWheel = (event: React.WheelEvent<HTMLImageElement>) => {
    Object.assign(pending.current, at(event));
    pending.current.deltaY += event.deltaY;
    later();
  };
  const pointer = (type: "down" | "move" | "up", event: React.PointerEvent<HTMLImageElement>) =>
    send({ type, ...at(event), button: held.current.button, clicks: type === "move" ? 1 : held.current.clicks });

  const release = (event: React.PointerEvent<HTMLImageElement>) => {
    if (!held.current.down) return;
    held.current.down = false;
    pointer("up", event);
  };

  const macInput = {
    tabIndex: 0,
    onPointerDown: (event: React.PointerEvent<HTMLImageElement>) => {
      event.currentTarget.focus();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      flush();
      const last = held.current;
      const button = BUTTONS[event.button] ?? "left";
      const now = Date.now();
      const again = button === last.button && now - last.t <= CLICK_MS
        && Math.hypot(event.clientX - last.x, event.clientY - last.y) <= CLICK_SLOP_PX;
      held.current = { button, clicks: again ? Math.min(3, last.clicks + 1) : 1, down: true, t: now, x: event.clientX, y: event.clientY };
      pointer("down", event);
    },
    // Only while a button is held: drags, selection and sliders (no hover).
    onPointerMove: (event: React.PointerEvent<HTMLImageElement>) => { if (event.buttons && held.current.down) pointer("move", event); },
    onPointerUp: release,
    // A touch that turns into a pan or zoom ends in a cancel, never an up: release the Mac's button.
    onPointerCancel: release,
    onLostPointerCapture: release,
    onContextMenu: (event: React.MouseEvent<HTMLImageElement>) => event.preventDefault(),
    onWheel,
    onKeyDown: (event: React.KeyboardEvent<HTMLImageElement>) => macKeyDown(event),
    onPaste: macPaste,
  };

  // No input onto a frame without a size: its pixels cannot be mapped.
  const input = !frame || !(frame.w > 0 && frame.h > 0) ? {} : macLike ? macInput : interactive ? {
    tabIndex: 0,
    onClick: (event: React.MouseEvent<HTMLImageElement>) => {
      event.currentTarget.focus();
      flush();
      send({ type: "click", ...at(event), button: event.button === 1 ? "middle" : "left" });
    },
    onWheel,
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
          alt={session ? "The session's browser tab, live" : "The agent's browser tab, live"}
          draggable={false}
          className={`h-full w-full select-none object-contain ${macLike ? "touch-none " : ""}${live ? "cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-[var(--bulb)]" : ""}`}
          {...input}
        />
      ) : (
        empty ?? <p role="status" className="text-sm text-[var(--midnight-muted)]">Waiting for the agent&apos;s page…</p>
      )}
      {macLike && frame && (
        <input key={`${runId ?? session?.sessionId}:${deviceId}`} ref={macEditor} type="text"
          aria-label={session ? "Type into the page" : "Type on Mac"} placeholder={session ? "Type into the page" : "Type on Mac"}
          autoComplete="off" autoCapitalize="off" spellCheck={false}
          className="absolute right-2 top-2 z-10 w-36 rounded-md border border-[var(--edge)] bg-[var(--cream)] px-2 py-1 text-sm text-[var(--midnight)]"
          onKeyDown={(event) => macKeyDown(event, true)} onPaste={macPaste}
          onBeforeInput={(event) => {
            if (!composing.current && macInputKey((event.nativeEvent as InputEvent).inputType)) event.preventDefault();
          }}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={(event) => {
            composing.current = false;
            const target = event.currentTarget;
            const committed = target.value;
            if (committed) {
              compositionEcho.current = committed;
              commitMacText(target);
              // ponytail: 150 ms echo window; track beforeinput sequence if delayed IME commits appear.
              setTimeout(() => { if (compositionEcho.current === committed) compositionEcho.current = null; }, INPUT_BATCH_MS);
            }
          }}
          onInput={macTextInput} />
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
