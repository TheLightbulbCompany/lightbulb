/**
 * The DCV client assigns `navigator.mediaDevices = {}` when the API is absent.
 * WKWebView exposes that missing property as read-only, so its module evaluation
 * throws before the live viewer can connect.
 */
export function ensureDcvMediaShim(): boolean {
  if (typeof navigator === "undefined" || navigator.mediaDevices !== undefined) return true;
  try {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      enumerable: true,
      value: {},
      writable: true,
    });
    return true;
  } catch {
    return false;
  }
}
