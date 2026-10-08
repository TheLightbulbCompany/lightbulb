import type { SessionScreen } from "./types";

/** A read-only terminal's place in the Mac's frame stream (spec §5.2-5.3). Within one relay run
 *  (`epoch`) every frame but a ping takes one `seq`. A viewer writes from that run's snapshot on,
 *  in seq order; a viewer with nothing drawn in the run first writes the newest unbroken run of its
 *  `history` frames below the snapshot. So does a viewer that draws the run but has written no
 *  history yet (`bare`): a snapshot can reach it before the burst its own watch asked for (a view
 *  that mounted in a hidden tab draws what another tab's watch brings), and a burst can be lost.
 *  That burst then starts its picture over, history first. A viewer with history written consumes
 *  later bursts in order and writes none: they are for someone else, and its scrollback stays.
 *  Frames can arrive out of order or never: each is its own request from the Mac to here, so a
 *  burst's frames can come after the snapshot the Mac sent behind them. Up to 16 early ones are
 *  held; past that the view waits for the next snapshot. History from below the snapshot it goes
 *  under (`shown`) starts the picture over: the longer run, then everything written since. */
export type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[]; history: SessionScreen[];
  /** No history is written in this run yet. An empty history frame counts: the Mac has none to give. */
  bare: boolean;
  /** What is written since the snapshot this viewer's history goes under, that snapshot first: the
   *  picture to write again under history that arrives after it. Empty once it is more than SHOWN
   *  frames: history that late is not written. */
  shown: SessionScreen[] };
export const NEW_SCREEN: ScreenOrder = { epoch: -1, next: 0, synced: false, held: [], history: [], bare: true, shown: [] };
const HELD = 16;
/** A quiet screen fills this in minutes, a printing one in seconds; a late frame is late by less (a cold relay Lambda took 2.7 s, prod 2026-10-08). */
const SHOWN = 32;
/** ceil(256 KiB / 11 KiB): one history burst at most (the Mac's BURST over CHUNK). */
const HISTORY = 24;

export function receiveScreen(order: ScreenOrder, frame: SessionScreen): { order: ScreenOrder; write: SessionScreen[] } {
  if (frame.kind === "ping") return { order, write: [] };
  if (frame.kind === "snapshot") {
    const old = frame.epoch < order.epoch || (frame.epoch === order.epoch && order.synced && frame.seq < order.next);
    if (old) return { order, write: [] };
    // History only for a viewer with none of this run's: scrollback it has stays as it is.
    const wants = order.bare || frame.epoch !== order.epoch;
    const history = order.history.filter((f) => f.epoch >= frame.epoch);
    const before = wants ? newestRun(history, frame) : [];
    return drain({ epoch: frame.epoch, next: frame.seq + 1, synced: true, history, bare: wants && !before.length, shown: order.shown,
      held: order.held.filter((f) => f.epoch === frame.epoch && f.seq > frame.seq) }, [...before, frame], wants);
  }
  if (order.synced && frame.epoch === order.epoch) {
    if (frame.seq < order.next) return frame.kind === "history" ? late(order, frame) : { order, write: [] };
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
  return add(order.history, frame);
}

function add(history: SessionScreen[], frame: SessionScreen) {
  const kept = history.filter((f) => f.epoch >= frame.epoch);
  return kept.some((f) => f.epoch > frame.epoch || f.seq === frame.seq) ? history : [...kept, frame].slice(-HISTORY);
}

/** A history frame from below this viewer's place in the run it draws: its burst came after the
 *  snapshot the Mac sent behind it (prod 2026-10-08: once by 2.6 s; about half of all opens kept
 *  no scrollback). When it makes the run under that snapshot longer or nearer it, the picture starts over:
 *  that run, then all that is written since. A viewer whose history is whole gets none (its burst's
 *  frames are all here), so nobody else's open redraws it. */
function late(order: ScreenOrder, frame: SessionScreen) {
  const base = order.shown[0];
  // Nothing to write it under now: a bare viewer keeps it for the next snapshot.
  if (!base) return { order: order.bare ? { ...order, history: add(order.history, frame) } : order, write: [] };
  // (A newer run's history is already here: this picture is about to be replaced.)
  if (frame.seq > base.seq || order.history.some((f) => f.epoch !== order.epoch)) return { order, write: [] };
  const history = add(order.history, frame);
  const [had, run] = [newestRun(order.history, base), newestRun(history, base)];
  // Another run only when it is longer or ends nearer the snapshot: a chunk that joins nothing changes nothing.
  const newer = run.length > 0 && (run.length > had.length || run[run.length - 1].seq > had[had.length - 1].seq);
  if (!newer) return { order: { ...order, history }, write: [] };
  return { order: { ...order, history, bare: false }, write: [...run, ...order.shown] };
}

/** The snapshot's history frames below it, newest unbroken run only: a lost chunk costs the older part. */
function newestRun(history: SessionScreen[], snapshot: SessionScreen) {
  const mine = history.filter((f) => f.epoch === snapshot.epoch && f.seq < snapshot.seq).sort((a, b) => a.seq - b.seq);
  let start = mine.length - 1;
  while (start > 0 && mine[start - 1].seq === mine[start].seq - 1) start -= 1;
  return mine.slice(Math.max(start, 0));
}

/** `under`: the snapshot in `write` is the one this viewer's history goes under from now on. */
function drain(order: ScreenOrder, write: SessionScreen[], under = false) {
  let { next, held } = order;
  for (;;) {
    const i = held.findIndex((f) => f.epoch === order.epoch && f.seq === next);
    if (i < 0) break;
    // A history frame in a run this viewer already draws: its place in the order, nothing to write.
    if (held[i].kind !== "history") write.push(held[i]);
    held = held.filter((_, j) => j !== i); next += 1;
  }
  const drawn = write.filter((f) => f.kind !== "history");
  const shown = under ? drawn : order.shown.length ? [...order.shown, ...drawn] : [];
  return { order: { ...order, next, held, shown: shown.length > SHOWN ? [] : shown }, write };
}
