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
 *  under (`shown`) starts the picture over: the longer run, then everything written since.
 *  A snapshot that comes ahead of this viewer's place in a run it draws, with a lower frame not
 *  here yet, waits in `held`: written at once it would cover the screen and the late output under
 *  it would be dropped, and lines that output scrolled off the top never reach the scrollback (an
 *  inline Codex session writes its finished lines that way). It is written in its place when the
 *  lower frames come, or as it is when the view's timer says they will not (`settleScreen`,
 *  SNAPSHOT_WAIT_MS): then it is a repair, as before. One rule with output ahead of a gap, which is
 *  held the same way. Never the first picture of a viewer or a run, nor a viewer out of sync.
 *  A snapshot written as a repair leaves this viewer `torn`: the frames it replaced never came, so
 *  the lines their output scrolled off the top are in no scrollback of this viewer (prod
 *  2026-10-08: 82 rows, 4 Codex blocks). The step says `lost`, the view asks the Mac for a fresh
 *  watch, and the history burst that follows starts this viewer's picture over, as a bare one's
 *  does. Nobody else writes that burst. */
export type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[]; history: SessionScreen[];
  /** No history is written in this run yet. An empty history frame counts: the Mac has none to give. */
  bare: boolean;
  /** What is written since the snapshot this viewer's history goes under, that snapshot first: the
   *  picture to write again under history that arrives after it. Empty once it is more than SHOWN
   *  frames: history that late is not written. */
  shown: SessionScreen[];
  /** A snapshot repaired this run's picture over frames that never came: lines are missing above
   *  the screen until a history burst is written again, in order or late (`late`). An empty burst
   *  ends it with nothing written: there are no lines to give. */
  torn?: boolean };
export const NEW_SCREEN: ScreenOrder = { epoch: -1, next: 0, synced: false, held: [], history: [], bare: true, shown: [] };
/** How long a snapshot waits for the frames below it. Frames of one burst reached the backend up to
 *  150 ms out of order (prod 2026-10-08, a snapshot ahead of 12 history chunks) and up to 184 ms in
 *  #3876's record: this is past both with room. A cold relay Lambda (2.7 s, once) is not waited
 *  for: the snapshot is then the repair it always was. Only a viewer missing a frame ever waits,
 *  but that includes every viewer of a session that just ended (the backend drops the frames
 *  before the kept picture), so the wait is kept short: it is added to that picture's delay. */
export const SNAPSHOT_WAIT_MS = 300;
const HELD = 16;
/** A quiet screen fills this in minutes, a printing one in seconds; a late frame is late by less (a cold relay Lambda took 2.7 s, prod 2026-10-08). */
const SHOWN = 32;
/** ceil(256 KiB / 11 KiB): one history burst at most (the Mac's BURST over CHUNK). */
const HISTORY = 24;

/** `lost`: a snapshot was written and this viewer still misses lines above it (`ScreenOrder.torn`,
 *  or a bare viewer that lost frames): the view may ask the Mac for them. */
type Step = { order: ScreenOrder; write: SessionScreen[]; lost?: boolean };

export function receiveScreen(order: ScreenOrder, frame: SessionScreen): Step {
  let step = receive(order, frame);
  for (;;) {
    // A held snapshot's turn: everything below it is here now.
    const ready = heldSnapshots(step.order).find((s) => !waits(step.order, s));
    if (!ready) return step;
    const drawn = draw(step.order, ready);
    step = { order: drawn.order, write: [...step.write, ...drawn.write], lost: step.lost || drawn.lost };
  }
}

/** Whether a snapshot waits for frames below it: the view then calls `settleScreen` in SNAPSHOT_WAIT_MS. */
export const snapshotWaits = (order: ScreenOrder) => heldSnapshots(order).length > 0;

/** The frames below the waiting snapshot did not come in time: write the newest one as it is. */
export function settleScreen(order: ScreenOrder): Step {
  const newest = heldSnapshots(order).pop();
  return newest ? draw(order, newest) : { order, write: [] };
}

function heldSnapshots(order: ScreenOrder) {
  return order.held.filter((f) => f.kind === "snapshot" && f.epoch >= order.epoch).sort((a, b) => a.seq - b.seq);
}

/** Whether a frame below `snapshot` in the run this viewer draws in sync has not arrived. */
function missing(order: ScreenOrder, snapshot: SessionScreen) {
  if (!order.synced || snapshot.epoch !== order.epoch) return false;
  if (snapshot.seq - order.next > HELD + HISTORY) return true;
  const here = new Set([...order.held, ...order.history].filter((f) => f.epoch === order.epoch).map((f) => f.seq));
  for (let seq = order.next; seq < snapshot.seq; seq += 1) if (!here.has(seq)) return true;
  return false;
}

/** A snapshot whose later parts (`SessionScreen.parts`) are not all held: written alone, its first
 *  part clears the screen and the rest paint later, the flash this field exists to end. */
function partial(order: ScreenOrder, snapshot: SessionScreen) {
  const here = new Set(order.held.filter((f) => f.epoch === snapshot.epoch).map((f) => f.seq));
  for (let seq = snapshot.seq + 1; seq < snapshot.seq + (snapshot.parts ?? 1); seq += 1) if (!here.has(seq)) return true;
  return false;
}
const waits = (order: ScreenOrder, s: SessionScreen) => missing(order, s) || partial(order, s);

function draw(order: ScreenOrder, frame: SessionScreen): Step {
  const same = frame.epoch === order.epoch;
  // A repair: this run's picture goes on from the screen alone, over frames that never came.
  const lost = same && (!order.synced || missing(order, frame));
  // History only for a viewer with none of this run's, or with a hole in it: scrollback that is whole stays as it is.
  const fresh = order.bare || !same;
  const was = same && !!order.torn;
  // (The burst a viewer was first drawn from is not its history once it is torn; what came since is.)
  const history = (lost && !fresh && !was ? [] : order.history).filter((f) => f.epoch >= frame.epoch);
  let before = fresh || was ? newestRun(history, frame) : [];
  // Torn until a burst is here: its lines are written, or it is empty (nothing above the screen),
  // which is no reason to drop the lines this viewer has.
  let torn = !fresh && (was || lost);
  if (torn && before.length) {
    torn = false;
    if (before.every((f) => !f.data)) before = [];
  }
  // (`under`: a torn viewer's history goes under the newest snapshot it has, so one that comes late can.)
  const step = drain({ epoch: frame.epoch, next: frame.seq + 1, synced: true, history, bare: fresh && !before.length, shown: order.shown,
    torn: torn || undefined,
    held: order.held.filter((f) => f.epoch === frame.epoch && f.seq > frame.seq) }, [...before, frame], before.length > 0 || fresh || torn);
  // Said at every snapshot until the lines are here: the view asks again, at its own pace.
  return torn || (lost && !before.length) ? { ...step, lost: true } : step;
}

function receive(order: ScreenOrder, frame: SessionScreen): Step {
  if (frame.kind === "ping") return { order, write: [] };
  if (frame.kind === "snapshot") {
    const old = frame.epoch < order.epoch || (frame.epoch === order.epoch && order.synced && frame.seq < order.next);
    if (old) return { order, write: [] };
    const held = order.held.some((f) => f.epoch === frame.epoch && f.seq === frame.seq) ? order.held : [...order.held, frame];
    // Held like output ahead of a gap; with no room left to hold it, written now.
    const next = { ...order, held };
    return waits(next, frame) && held.length <= HELD ? { order: next, write: [] } : draw(order, frame);
  }
  if (order.synced && frame.epoch === order.epoch) {
    if (frame.seq < order.next) return frame.kind === "history" ? late(order, frame) : { order, write: [] };
    if (frame.seq === order.next) return drain({ ...order, next: order.next + 1, history: stash(order, frame) }, frame.kind === "history" ? [] : [frame]);
  }
  if (frame.kind === "history" && (frame.epoch > order.epoch || ((order.bare || order.torn) && frame.epoch === order.epoch))) {
    return { order: { ...order, history: stash(order, frame) }, write: [] };
  }
  const held = [...order.held, frame];
  if (held.length <= HELD) return { order: { ...order, held }, write: [] };
  // No room to wait any longer: a snapshot that was waiting is the picture now, with what is held above it.
  const newest = heldSnapshots({ ...order, held }).pop();
  return newest ? draw({ ...order, held }, newest) : { order: { ...order, synced: false, held: [] }, write: [] };
}

/** The history kept for the next snapshot, with `frame` when it is history this viewer may yet write. */
function stash(order: ScreenOrder, frame: SessionScreen) {
  if (frame.kind !== "history" || !(order.bare || order.torn || frame.epoch > order.epoch)) return order.history;
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
  // Nothing to write it under now: a bare viewer keeps it for the next snapshot, and so does a torn
  // one (its repair burst came after the snapshot behind it, and more than SHOWN frames since).
  if (!base) return { order: order.bare || order.torn ? { ...order, history: add(order.history, frame) } : order, write: [] };
  // (A newer run's history is already here: this picture is about to be replaced.)
  if (frame.seq > base.seq || order.history.some((f) => f.epoch !== order.epoch)) return { order, write: [] };
  const history = add(order.history, frame);
  const [had, run] = [newestRun(order.history, base), newestRun(history, base)];
  // Another run only when it is longer or ends nearer the snapshot: a chunk that joins nothing changes nothing.
  const newer = run.length > 0 && (run.length > had.length || run[run.length - 1].seq > had[had.length - 1].seq);
  if (!newer) return { order: { ...order, history }, write: [] };
  // (An empty burst gives a torn viewer nothing to start over from: it keeps its lines.)
  if (order.torn && !order.bare && run.every((f) => !f.data)) return { order: { ...order, history, torn: undefined }, write: [] };
  return { order: { ...order, history, bare: false, torn: undefined }, write: [...run, ...order.shown] };
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
    // A snapshot keeps its place until it is drawn (`receiveScreen`).
    if (held[i].kind === "snapshot") break;
    // A history frame in a run this viewer already draws: its place in the order, nothing to write.
    if (held[i].kind !== "history") write.push(held[i]);
    held = held.filter((_, j) => j !== i); next += 1;
  }
  const drawn = write.filter((f) => f.kind !== "history");
  const shown = under ? drawn : order.shown.length ? [...order.shown, ...drawn] : [];
  return { order: { ...order, next, held, shown: shown.length > SHOWN ? [] : shown }, write };
}
