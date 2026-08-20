// Pure canvas-coords -> CDP-input mapping for the screencast panel. Kept
// import-free and side-effect-free (like browserViewUrl.ts) so the load-bearing
// coordinate math is unit-testable without mounting the panel or a real canvas.
//
// The frontend owns coord mapping; the backend just routes {type} -> CDP
// Input.* method. A screencast frame carries metadata.deviceWidth/deviceHeight
// (the page's CSS viewport size). The JPEG's natural pixel size cancels out of
// the mapping, so we only need: the pointer position within the displayed
// canvas, the canvas's displayed size, and the frame metadata.
//
//   cssX = (pointerX_in_canvas / displayedWidth)  * deviceWidth
//   cssY = (pointerY_in_canvas / displayedHeight) * deviceHeight
//
// CDP Input.dispatchMouseEvent wants x,y in CSS px of the layout viewport, which
// is exactly cssX/cssY. See puppeteer/chrome-remote-interface screencast viewers.

/** The subset of Page.screencastFrame metadata we need to map input. */
export type ScreencastMetadata = {
  deviceWidth: number;
  deviceHeight: number;
};

/** A displayed rectangle (canvas getBoundingClientRect) + a client point. */
export type CanvasRect = { left: number; top: number; width: number; height: number };

/** Map a viewport-space pointer (clientX/clientY) to CDP CSS-px device coords. */
export function mapPointerToDevice(
  clientX: number,
  clientY: number,
  rect: CanvasRect,
  meta: ScreencastMetadata,
): { x: number; y: number } {
  // Guard against a zero-sized canvas (pre-layout) -> avoid NaN/Infinity.
  if (rect.width <= 0 || rect.height <= 0) return { x: 0, y: 0 };
  const relX = clientX - rect.left;
  const relY = clientY - rect.top;
  // Clamp into the canvas so an off-by-a-pixel edge drag can't send negative or
  // out-of-page coords to CDP.
  const fracX = Math.min(1, Math.max(0, relX / rect.width));
  const fracY = Math.min(1, Math.max(0, relY / rect.height));
  return {
    x: Math.round(fracX * meta.deviceWidth),
    y: Math.round(fracY * meta.deviceHeight),
  };
}

// Which DOM MouseEvent.button -> CDP button name. CDP wants "none" for moves.
const CDP_BUTTONS = ["left", "middle", "right", "back", "forward"] as const;

/** Shape a DOM mouse event into CDP Input.dispatchMouseEvent params. */
export function toCdpMouseEvent(
  type: "mousePressed" | "mouseReleased" | "mouseMoved" | "mouseWheel",
  pos: { x: number; y: number },
  opts: { button?: number; buttons?: number; clickCount?: number; deltaX?: number; deltaY?: number } = {},
): Record<string, unknown> {
  const isButtonAction = type === "mousePressed" || type === "mouseReleased";
  const event: Record<string, unknown> = {
    type,
    x: pos.x,
    y: pos.y,
    button: isButtonAction ? CDP_BUTTONS[opts.button ?? 0] ?? "left" : "none",
    buttons: opts.buttons ?? 0,
  };
  if (isButtonAction) event.clickCount = opts.clickCount ?? 1;
  if (type === "mouseWheel") {
    event.deltaX = opts.deltaX ?? 0;
    event.deltaY = opts.deltaY ?? 0;
  }
  return event;
}

/** Shape a DOM keyboard event into CDP Input.dispatchKeyEvent params.
 *
 * Phase-1 fidelity: key/code/keyCode + `text` for printable keydowns (enough to
 * type and use shortcuts). Full modifiers/IME are Phase 4 per the plan. */
export function toCdpKeyEvent(
  type: "keyDown" | "keyUp",
  ev: { key: string; code: string; keyCode: number },
): Record<string, unknown> {
  // A single printable character is `text`; named keys (Enter, Tab, arrows)
  // have key.length > 1 and carry no text.
  const printable = type === "keyDown" && ev.key.length === 1;
  const event: Record<string, unknown> = {
    type,
    key: ev.key,
    code: ev.code,
    windowsVirtualKeyCode: ev.keyCode,
    nativeVirtualKeyCode: ev.keyCode,
  };
  if (printable) {
    event.text = ev.key;
    // "char"-less keyDown with text is accepted by CDP for typing; Enter still
    // needs its \r though.
  } else if (type === "keyDown" && ev.key === "Enter") {
    event.text = "\r";
  }
  return event;
}
