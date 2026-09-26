/**
 * Fit the remote display into the panel, top-aligned: full width whenever the
 * scaled height fits, so the stream reads as the panel's content rather than a
 * centred picture. When the panel is wider than the remote's aspect, fit to
 * height (never crop the remote viewport) and centre horizontally.
 *
 * ponytail: the remote viewport is fixed at session start (the agent's
 * automation coordinates depend on it, and the AgentCore SDK itself locks the
 * DCV display layout), so a panel of another aspect keeps one band of panel
 * background — below or beside. Resizing the remote is the upgrade path.
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
