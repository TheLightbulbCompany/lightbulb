/**
 * Fit the remote display into the panel, top-aligned: full width whenever the
 * scaled height fits, so the stream reads as the panel's content rather than a
 * centred picture. When the panel is wider than the remote's aspect, fit to
 * height (never crop the remote viewport) and centre horizontally.
 *
 * The viewer asks the remote display to take the panel's own size (see
 * `layoutRequester`), so once the server confirms, this is a 1:1 fit. It still
 * scales while a resize is in flight, and when the panel is outside the
 * requestable range.
 */
export function fitDcvDisplay(
  containerWidth: number,
  containerHeight: number,
  remoteWidth: number,
  remoteHeight: number,
): { scale: number; offsetX: number; offsetY: number } {
  if (containerWidth <= 0 || remoteWidth <= 0 || remoteHeight <= 0) {
    return { scale: 0, offsetX: 0, offsetY: 0 };
  }
  // A container whose height the engine cannot resolve (WKWebView reported 0 for a
  // percentage height inside a flex item; prod desktop app 2026-09-26) still fits its
  // width: a 1:1 remote would crop the right side instead.
  const byWidth = containerWidth / remoteWidth;
  const scale = containerHeight > 0 ? Math.min(byWidth, containerHeight / remoteHeight) : byWidth;
  return { scale, offsetX: (containerWidth - remoteWidth * scale) / 2, offsetY: 0 };
}

export interface Size { width: number; height: number }

/** The range the AgentCore display server reports for a web client (min 640x480; the web
 * client's max head is 1920x1080, below the server's own 4096x2160). Dev, 2026-09-26. */
export const MIN_LAYOUT: Size = { width: 640, height: 480 };
export const MAX_LAYOUT: Size = { width: 1920, height: 1080 };
/** The server rounds each dimension down to a multiple of 8 (asked 1136x651, got 1136x648). */
const GRID = 8;
/** A panel drag fires dozens of resizes; the remote re-lays-out the page on each one. */
export const LAYOUT_DEBOUNCE_MS = 400;

/** The remote display size for a panel of this CSS size, as the server will grant it:
 * clamped and on its 8-pixel grid, so "already this size" compares equal. Null if unmeasured. */
export function panelLayout(width: number, height: number): Size | null {
  if (width <= 0 || height <= 0) return null;
  // Scale out of range as a whole, keeping the panel's aspect, so the fit fills it edge to edge.
  const factor = Math.min(
    Math.max(1, MIN_LAYOUT.width / width, MIN_LAYOUT.height / height),
    MAX_LAYOUT.width / width,
    MAX_LAYOUT.height / height,
  );
  const snap = (value: number, min: number, max: number) =>
    Math.min(max, Math.max(min, Math.floor((value * factor) / GRID) * GRID));
  return {
    width: snap(width, MIN_LAYOUT.width, MAX_LAYOUT.width),
    height: snap(height, MIN_LAYOUT.height, MAX_LAYOUT.height),
  };
}

/**
 * Debounced "make the remote display the panel's size". Waits out a resize burst, drops the
 * request while `busy()` (the browser is changing control, so a reconnect and a fresh
 * request follow; the caller re-schedules when busy clears) and skips a request for the
 * size the remote already has.
 */
export function layoutRequester({ request, current, busy, delayMs = LAYOUT_DEBOUNCE_MS }: {
  request: (size: Size) => void;
  current: () => Size | null;
  busy: () => boolean;
  delayMs?: number;
}): { schedule: (width: number, height: number) => void; cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const cancel = () => clearTimeout(timer);
  const schedule = (width: number, height: number) => {
    cancel();
    timer = setTimeout(() => {
      if (busy()) return;
      const next = panelLayout(width, height);
      const now = current();
      if (!next || (now && now.width === next.width && now.height === next.height)) return;
      request(next);
    }, delayMs);
  };
  return { schedule, cancel };
}
