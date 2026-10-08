import {
  NEW_SCREEN, SNAPSHOT_WAIT_MS, chunkInput, dropMouseReports, dropProbeReplies, inputRefused, receiveScreen, settleScreen, snapshotWaits, toBase64,
  type SessionScreen,
} from "@lightbulb/shared";

/** A snapshot or a ping comes at least every 10 s; this long with no frame, or only pings that do not
 *  match the screen it has, and the picture is frozen, so the view says Reconnecting (the web's value). */
export const STALE_AFTER_MS = 30_000;
export const WATCH_EVERY_MS = 15_000;
/** A viewer that lost frames asks the Mac for its lines again at most this often: each ask is a history burst out of the session's budget. */
export const REPAIR_EVERY_MS = 30_000;
/** Clears the visible screen only, so scrollback stays; first leaves the alternate screen and drops
 *  the scroll region. Written ahead of every snapshot, as on the web. base64 of ESC[?1049l ESC[r ESC[2J ESC[H. */
export const CLEAR = "G1s/MTA0OWwbW3IbWzJKG1tI";

/** One thing for the WebView's page to do (scripts/terminal-page.js `draw`), in order.
 *  `fresh`: start the terminal over first; `again`: the same relay run, drawn again under history
 *  that came late; `resize`: the grid changed; `back`: put a reader who had scrolled up back on
 *  their line once this is written. `data`: base64 parts, written in order. */
export type TerminalStep = { fresh: boolean; again: boolean; resize: boolean; cols: number; rows: number; back: boolean; data: string[] };

/** The phone's place in a session's frame stream: the web view's event handler and `apply`
 *  (SessionTerminalView.tsx) without xterm. Ordering, the snapshot hold and the history redraw are
 *  the shared `receiveScreen` / `settleScreen`; this turns what they say to write into steps. */
export function createScreenFeed({ draw, onLive, onSeen, onLost }: {
  draw: (steps: TerminalStep[]) => void; onLive: (live: boolean) => void; onSeen: () => void;
  /** A snapshot repaired the screen over frames that never came, so lines are missing above it: the
   *  view asks for one fresh watch, whose history burst starts the picture over with them. Called at
   *  most once in REPAIR_EVERY_MS. */
  onLost?: () => void;
}) {
  let order = NEW_SCREEN;
  let drawn = -1; // the relay run on screen; a new one starts the terminal over
  let wrote = ""; // the kind of the frame written last
  let sized = ""; // the size asked for last
  let stale: ReturnType<typeof setTimeout> | undefined;
  // Runs while a snapshot waits for the frames below it; from the first one that waits.
  let settle: ReturnType<typeof setTimeout> | undefined;
  let repaired = 0;
  const repair = (lost?: boolean) => {
    if (!lost || Date.now() - repaired < REPAIR_EVERY_MS) return;
    repaired = Date.now();
    onLost?.();
  };
  const apply = (frames: SessionScreen[]) => {
    if (!frames.length) return;
    let over = false; // the picture started over in this batch: its snapshot puts a reader back
    draw(frames.map((frame) => {
      // History is only ever written as a burst under its snapshot, and the burst is the whole picture.
      const fresh = frame.epoch !== drawn || (frame.kind === "history" && wrote !== "history");
      const again = frame.epoch === drawn;
      const size = `${frame.cols}x${frame.rows}`;
      const resize = size !== sized;
      if (fresh) { drawn = frame.epoch; over = true; }
      wrote = frame.kind; sized = size;
      const snapshot = frame.kind === "snapshot";
      if (snapshot) { onLive(true); onSeen(); }
      // Only the snapshot of a picture that started over: the 30 s resync moves nobody.
      const back = snapshot && over;
      if (back) over = false;
      return { fresh, again, resize, cols: frame.cols, rows: frame.rows, back, data: snapshot ? [CLEAR, frame.data] : [frame.data] };
    }));
  };
  return {
    /** The relay run keys typed now are for. */
    epoch: () => order.epoch,
    receive(frame: SessionScreen) {
      // A ping proves the picture is current only when it names the frame this view waits for.
      const current = frame.kind === "ping" && order.synced && frame.epoch === order.epoch && frame.seq === order.next;
      if (frame.kind !== "ping" || current) {
        clearTimeout(stale);
        stale = setTimeout(() => onLive(false), STALE_AFTER_MS);
      }
      if (current) onLive(true);
      const step = receiveScreen(order, frame);
      order = step.order;
      apply(step.write);
      repair(step.lost);
      if (!snapshotWaits(order)) { clearTimeout(settle); settle = undefined; }
      else settle ??= setTimeout(() => { settle = undefined; const late = settleScreen(order); order = late.order; apply(late.write); repair(late.lost); }, SNAPSHOT_WAIT_MS);
    },
    dispose() { clearTimeout(stale); clearTimeout(settle); },
  };
}

/** Ctrl with the next key: letters and @ [ \ ] ^ _ become their control byte; anything else is typed as it is. */
export const withCtrl = (data: string): string => {
  if (data.length !== 1) return data;
  const code = data.toUpperCase().charCodeAt(0);
  return data === "?" ? "\x7f" : code >= 0x40 && code <= 0x5f ? String.fromCharCode(code & 0x1f) : data;
};

/** What the terminal's page hands back as typed goes to the Mac: never a mouse report or one of
 *  xterm's own probe replies, in calls of at most 4096 bytes, one at a time so keys stay in order,
 *  each with the screen epoch it was typed against. A refusal drops what is queued behind it. */
export function createTypist({ send, epoch, canType, onRefused }: {
  send: (data: string, epoch: number) => Promise<unknown>; epoch: () => number; canType: () => boolean; onRefused: () => void;
}) {
  let sending: Promise<unknown> = Promise.resolve();
  let ctrl = false;
  return {
    /** Sticky Ctrl from the key bar: applies to the next key, then lets go. */
    setCtrl(on: boolean) { ctrl = on; },
    /** Returns whether Ctrl was used up, so the bar can show it released. */
    type(raw: string): boolean {
      let data = dropMouseReports(dropProbeReplies(raw));
      if (!data || !canType()) return false;
      const at = epoch();
      // No screen yet: the input route refuses an epoch below 0, and half a command must not land later.
      if (at < 0) return false;
      const used = ctrl;
      if (ctrl) { data = withCtrl(data); ctrl = false; }
      for (const part of chunkInput(new TextEncoder().encode(data))) {
        sending = sending.then(() => (canType() ? send(toBase64(part), at) : undefined)).catch((e) => { if (inputRefused(e)) onRefused(); });
      }
      return used;
    },
  };
}
