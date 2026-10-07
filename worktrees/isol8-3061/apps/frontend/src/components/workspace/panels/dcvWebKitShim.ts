/**
 * The DCV client assigns `navigator.mediaDevices = {}` when the API is absent.
 * WKWebView exposes that missing property as read-only, so its module evaluation
 * throws before the live viewer can connect.
 */
export function ensureDcvMediaShim(): boolean {
  if (typeof navigator === "undefined") return true;
  try {
    if (!navigator.mediaDevices || typeof navigator.mediaDevices !== "object") {
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        enumerable: true,
        value: {},
        writable: true,
      });
    }
    return true;
  } catch {
    return false;
  }
}
