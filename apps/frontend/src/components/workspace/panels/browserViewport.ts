export const BROWSER_VIEWPORT_DEFAULT = { width: 1280, height: 800, rate: 24 };
export const BROWSER_VIEWPORT_MAX_AREA = 1_024_000;
export const BROWSER_VIEWPORT_STEP = 8;

export type BrowserViewport = typeof BROWSER_VIEWPORT_DEFAULT;

export function normalizeBrowserViewport(width: number, height: number): BrowserViewport {
  if (!(Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0)) {
    return { ...BROWSER_VIEWPORT_DEFAULT };
  }

  const scale = Math.min(1, Math.sqrt(BROWSER_VIEWPORT_MAX_AREA / (width * height)));
  let roundedWidth = Math.max(BROWSER_VIEWPORT_STEP, Math.floor((width * scale) / BROWSER_VIEWPORT_STEP) * BROWSER_VIEWPORT_STEP);
  let roundedHeight = Math.max(BROWSER_VIEWPORT_STEP, Math.floor((height * scale) / BROWSER_VIEWPORT_STEP) * BROWSER_VIEWPORT_STEP);
  if (roundedWidth * roundedHeight > BROWSER_VIEWPORT_MAX_AREA) {
    if (roundedWidth <= roundedHeight) {
      roundedHeight = Math.floor((BROWSER_VIEWPORT_MAX_AREA / roundedWidth) / BROWSER_VIEWPORT_STEP) * BROWSER_VIEWPORT_STEP;
    } else {
      roundedWidth = Math.floor((BROWSER_VIEWPORT_MAX_AREA / roundedHeight) / BROWSER_VIEWPORT_STEP) * BROWSER_VIEWPORT_STEP;
    }
  }
  return { width: roundedWidth, height: roundedHeight, rate: BROWSER_VIEWPORT_DEFAULT.rate };
}
