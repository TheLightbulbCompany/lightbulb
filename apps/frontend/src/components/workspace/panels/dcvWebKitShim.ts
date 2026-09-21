/**
 * The DCV client assigns `navigator.mediaDevices = {}` when the API is absent.
 * WKWebView exposes that missing property as read-only, so its module evaluation
 * throws before the live viewer can connect.
 */
export function ensureDcvMediaShim(): boolean {
  if (typeof navigator === "undefined") return true;
  try {
    // DCV mutates `mediaDevices.getUserMedia` at module evaluation time. A
    // WKWebView can expose a non-extensible native object with that method
    // absent, so give DCV an ordinary writable object before it imports.
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        enumerable: true,
        value: {
          getUserMedia: () => Promise.reject(new Error("Webcam is unavailable.")),
        },
        writable: true,
      });
    }
    return true;
  } catch {
    return false;
  }
}
