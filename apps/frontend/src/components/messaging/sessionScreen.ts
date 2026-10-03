import type { SessionScreen } from "@lightbulb/shared";

/** A read-only terminal's place in the Mac's frame stream. Frames are written in `seq` order
 *  within one relay run (`epoch`), and only after that run's snapshot. Frames can arrive out of
 *  order (each is its own request through the relay) or never (over budget, relay down): up to
 *  16 early ones are held; past that the view waits for the next snapshot, at most 10 s away. */
export type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[] };
export const NEW_SCREEN: ScreenOrder = { epoch: -1, next: 0, synced: false, held: [] };
const HELD = 16;

export function receiveScreen(order: ScreenOrder, frame: SessionScreen): { order: ScreenOrder; write: SessionScreen[] } {
  if (frame.kind === "snapshot") {
    const old = frame.epoch < order.epoch || (frame.epoch === order.epoch && order.synced && frame.seq < order.next);
    if (old) return { order, write: [] };
    return drain({ epoch: frame.epoch, next: frame.seq + 1, synced: true, held: order.held.filter((f) => f.epoch === frame.epoch && f.seq > frame.seq) }, [frame]);
  }
  if (order.synced && frame.epoch === order.epoch) {
    if (frame.seq < order.next) return { order, write: [] };
    if (frame.seq === order.next) return drain({ ...order, next: order.next + 1 }, [frame]);
  }
  const held = [...order.held, frame];
  return held.length > HELD ? { order: { ...order, synced: false, held: [] }, write: [] } : { order: { ...order, held }, write: [] };
}

function drain(order: ScreenOrder, write: SessionScreen[]) {
  let { next, held } = order;
  for (;;) {
    const i = held.findIndex((f) => f.epoch === order.epoch && f.seq === next);
    if (i < 0) break;
    write.push(held[i]); held = held.filter((_, j) => j !== i); next += 1;
  }
  return { order: { ...order, next, held }, write };
}
