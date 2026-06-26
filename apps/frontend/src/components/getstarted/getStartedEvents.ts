import type { StepId } from "./getStartedConfig";

// A process-local bus so producers (chat send, task create, hire success)
// can report a completed step without importing the guide or threading
// callbacks through deep hooks. SSR-safe: uses a module-level EventTarget.
const target = new EventTarget();
const EVENT = "getstarted:step";

export function emitStep(id: StepId): void {
  target.dispatchEvent(new CustomEvent(EVENT, { detail: id }));
}

export function onStep(cb: (id: StepId) => void): () => void {
  const handler = (e: Event) => cb((e as CustomEvent<StepId>).detail);
  target.addEventListener(EVENT, handler);
  return () => target.removeEventListener(EVENT, handler);
}
