import type { SessionScreen } from "./types";

/** A read-only terminal's place in the Mac's frame stream (spec §5.2-5.3). Within one relay run
 *  (`epoch`) every frame but a ping takes one `seq`. A viewer writes from that run's snapshot on,
 *  in seq order; a viewer with nothing drawn in the run first writes the newest unbroken run of its
 *  `history` frames below the snapshot. A viewer that already draws the run consumes history in
 *  order and writes none. Frames can arrive out of order or never: up to 16 early ones are held;
 *  past that the view waits for the next snapshot. */
export type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[]; history: SessionScreen[] };
export const NEW_SCREEN: ScreenOrder = { epoch: -1, next: 0, synced: false, held: [], history: [] };
const HELD = 16;
/** ceil(256 KiB / 11 KiB): one history burst at most (the Mac's BURST over CHUNK). */
const HISTORY = 24;

export function receiveScreen(order: ScreenOrder, frame: SessionScreen): { order: ScreenOrder; write: SessionScreen[] } {
  if (frame.kind === "ping") return { order, write: [] };
  if (frame.kind === "snapshot") {
    const old = frame.epoch < order.epoch || (frame.epoch === order.epoch && order.synced && frame.seq < order.next);
    if (old) return { order, write: [] };
    // History only for a run this viewer has drawn nothing of: scrollback it has stays as it is.
    const before = frame.epoch === order.epoch ? [] : newestRun(order.history, frame);
    return drain({ epoch: frame.epoch, next: frame.seq + 1, synced: true, history: [],
      held: order.held.filter((f) => f.epoch === frame.epoch && f.seq > frame.seq) }, [...before, frame]);
  }
  if (order.synced && frame.epoch === order.epoch) {
    if (frame.seq < order.next) return { order, write: [] };
    if (frame.seq === order.next) return drain({ ...order, next: order.next + 1 }, frame.kind === "history" ? [] : [frame]);
  }
  if (frame.kind === "history" && frame.epoch > order.epoch) {
    const kept = order.history.filter((f) => f.epoch >= frame.epoch);
    if (kept.some((f) => f.epoch > frame.epoch || f.seq === frame.seq)) return { order, write: [] };
    return { order: { ...order, history: [...kept, frame].slice(-HISTORY) }, write: [] };
  }
  const held = [...order.held, frame];
  return held.length > HELD ? { order: { ...order, synced: false, held: [] }, write: [] } : { order: { ...order, held }, write: [] };
}

/** The snapshot's history frames below it, newest unbroken run only: a lost chunk costs the older part. */
function newestRun(history: SessionScreen[], snapshot: SessionScreen) {
  const mine = history.filter((f) => f.epoch === snapshot.epoch && f.seq < snapshot.seq).sort((a, b) => a.seq - b.seq);
  let start = mine.length - 1;
  while (start > 0 && mine[start - 1].seq === mine[start].seq - 1) start -= 1;
  return mine.slice(Math.max(start, 0));
}

function drain(order: ScreenOrder, write: SessionScreen[]) {
  let { next, held } = order;
  for (;;) {
    const i = held.findIndex((f) => f.epoch === order.epoch && f.seq === next);
    if (i < 0) break;
    // A history frame in a run this viewer already draws: its place in the order, nothing to write.
    if (held[i].kind !== "history") write.push(held[i]);
    held = held.filter((_, j) => j !== i); next += 1;
  }
  return { order: { ...order, next, held }, write };
}
