/**
 * The DCV client assigns `navigator.mediaDevices = {}` when the API is absent.
 * WKWebView exposes that missing property as read-only, so its module evaluation
 * throws before the live viewer can connect.
 */
export function ensureDcvMediaShim(): boolean {
  if (typeof navigator === "undefined") return true;
  try {
    if (navigator.mediaDevices === undefined) {
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        enumerable: true,
        value: {},
        writable: true,
      });
    }
    const isTauri = typeof window !== "undefined" && ("__TAURI__" in window || "__TAURI_INTERNALS__" in window);
    if (isTauri || typeof navigator.mediaDevices.getUserMedia !== "function") {
      const emptyStream = {
        getAudioTracks: () => [],
        getTracks: () => [],
        getVideoTracks: () => [],
      } as unknown as MediaStream;
      Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
        configurable: true,
        value: async () => emptyStream,
        writable: true,
      });
    }
    return true;
  } catch {
    return false;
  }
}
