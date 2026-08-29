/** Feature-detect a usable WebGL context. Cheap, and it is the difference
 *  between a lit lamp and a blank rectangle on a locked-down machine.
 *
 *  Two steps, because they answer different questions: the constructor says
 *  whether this environment has WebGL at all (jsdom and headless renderers do
 *  not), and getContext says whether this GPU will actually hand one over —
 *  a blocklisted driver has the constructor and still refuses. Asking the
 *  constructor first also spares jsdom a canvas it would only warn about. */
export function hasWebGL(): boolean {
  if (typeof window === "undefined" || typeof document === "undefined") return false;
  if (!("WebGL2RenderingContext" in window) && !("WebGLRenderingContext" in window)) return false;
  try {
    const c = document.createElement("canvas");
    const ctx = c.getContext("webgl2") || c.getContext("webgl");
    if (!ctx) return false;
    // Contexts are a scarce per-tab resource (browsers cap them around 16) and
    // this one only ever answered a question. Hand it straight back, so the
    // probe is never why the scene's real context gets evicted.
    (ctx.getExtension("WEBGL_lose_context") as { loseContext(): void } | null)?.loseContext();
    return true;
  } catch {
    return false;
  }
}
