/** First retry after a dropped viewer, doubling up to the cap: 0.5 s, 1, 2, 4 … 30 s. */
export const RECONNECT_FIRST_MS = 500;
export const RECONNECT_MAX_MS = 30_000;
/** Retries keep going silently this long after a drop before the viewer says it is down. */
export const RECONNECT_GIVE_UP_MS = 30_000;

export function reconnectDelay(attempt: number): number {
  return Math.min(RECONNECT_MAX_MS, RECONNECT_FIRST_MS * 2 ** attempt);
}

export type ConnectReason = "session" | "drop" | "retry";

declare global {
  interface Window {
    /** Dev aid: every viewer connect, per browser session, with why it happened. */
    __lightbulbViewerConnects?: Record<string, { at: string; reason: ConnectReason }[]>;
  }
}

/** Count a viewer connect so flicker has a visible cause (`window.__lightbulbViewerConnects`). */
export function recordViewerConnect(sessionKey: string, reason: ConnectReason): number {
  const log = (window.__lightbulbViewerConnects ??= {});
  const entries = (log[sessionKey] ??= []);
  entries.push({ at: new Date().toISOString(), reason });
  console.info(`[browser viewer] connect #${entries.length} for session ${sessionKey} (${reason})`);
  return entries.length;
}
