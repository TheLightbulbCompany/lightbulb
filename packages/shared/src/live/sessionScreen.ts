import type { SessionScreen } from "./types";

/** A read-only terminal's place in the Mac's frame stream (spec §5.2-5.3). Within one relay run
 *  (`epoch`) every frame but a ping takes one `seq`. A viewer writes from that run's snapshot on,
 *  in seq order; a viewer with nothing drawn in the run first writes the newest unbroken run of its
 *  `history` frames below the snapshot. So does a viewer that draws the run but has written no
 *  history yet (`bare`): a snapshot can reach it before the burst its own watch asked for (a view
 *  that mounted in a hidden tab draws what another tab's watch brings), and a burst can be lost.
 *  That burst then starts its picture over, history first. A viewer with history written consumes
 *  later bursts in order and writes none: they are for someone else, and its scrollback stays.
 *  Frames can arrive out of order or never: up to 16 early ones are held; past that the view waits
 *  for the next snapshot. */
export type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[]; history: SessionScreen[];
  /** No history is written in this run yet. An empty history frame counts: the Mac has none to give. */
  bare: boolean };
export const NEW_SCREEN: ScreenOrder = { epoch: -1, next: 0, synced: false, held: [], history: [], bare: true };
const HELD = 16;
/** ceil(256 KiB / 11 KiB): one history burst at most (the Mac's BURST over CHUNK). */
const HISTORY = 24;

export function receiveScreen(order: ScreenOrder, frame: SessionScreen): { order: ScreenOrder; write: SessionScreen[] } {
  if (frame.kind === "ping") return { order, write: [] };
  if (frame.kind === "snapshot") {
    const old = frame.epoch < order.epoch || (frame.epoch === order.epoch && order.synced && frame.seq < order.next);
    if (old) return { order, write: [] };
    // History only for a viewer with none of this run's: scrollback it has stays as it is.
    const wants = order.bare || frame.epoch !== order.epoch;
    const before = wants ? newestRun(order.history, frame) : [];
    return drain({ epoch: frame.epoch, next: frame.seq + 1, synced: true, history: [], bare: wants && !before.length,
      held: order.held.filter((f) => f.epoch === frame.epoch && f.seq > frame.seq) }, [...before, frame]);
  }
  if (order.synced && frame.epoch === order.epoch) {
    if (frame.seq < order.next) return { order, write: [] };
    if (frame.seq === order.next) return drain({ ...order, next: order.next + 1, history: stash(order, frame) }, frame.kind === "history" ? [] : [frame]);
  }
  if (frame.kind === "history" && (frame.epoch > order.epoch || (order.bare && frame.epoch === order.epoch))) {
    return { order: { ...order, history: stash(order, frame) }, write: [] };
  }
  const held = [...order.held, frame];
  return held.length > HELD ? { order: { ...order, synced: false, held: [] }, write: [] } : { order: { ...order, held }, write: [] };
}

/** The history kept for the next snapshot, with `frame` when it is history this viewer may yet write. */
function stash(order: ScreenOrder, frame: SessionScreen) {
  if (frame.kind !== "history" || !(order.bare || frame.epoch > order.epoch)) return order.history;
  const kept = order.history.filter((f) => f.epoch >= frame.epoch);
  return kept.some((f) => f.epoch > frame.epoch || f.seq === frame.seq) ? order.history : [...kept, frame].slice(-HISTORY);
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
