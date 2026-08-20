import { describe, it, expect } from "vitest";
import { screencastWsUrl } from "../panels/browserViewUrl";
import { mapPointerToDevice, toCdpMouseEvent, toCdpKeyEvent } from "../panels/screencastInput";
import { normalizeBrowserUrlDraft } from "../panels/browserNav";
import { tabLabel } from "../panels/browserTabs";

// The screencast rides the SAME bv door as the VNC relay (its path is under
// /browser-view/*), so the host mapping is identical and just as load-bearing.
describe("screencastWsUrl", () => {
  it("maps the dev api host to the CloudFront bv host", () => {
    expect(screencastWsUrl("https://api-dev.isol8.co/api/v1")).toBe(
      "wss://bv-dev.isol8.co/api/v1/browser-view/screencast/ws",
    );
  });

  it("maps the prod api host to bv.isol8.co", () => {
    expect(screencastWsUrl("https://api.isol8.co/api/v1")).toBe(
      "wss://bv.isol8.co/api/v1/browser-view/screencast/ws",
    );
  });

  it("connects straight to a local backend (no CloudFront locally)", () => {
    expect(screencastWsUrl("http://localhost:8000/api/v1")).toBe(
      "ws://localhost:8000/api/v1/browser-view/screencast/ws",
    );
  });

  it("appends ?target= so the relay streams the tab the chrome switched to", () => {
    expect(screencastWsUrl("https://api-dev.isol8.co/api/v1", "ABC-123")).toBe(
      "wss://bv-dev.isol8.co/api/v1/browser-view/screencast/ws?target=ABC-123",
    );
    // No target ⇒ bare url (relay picks the first page target).
    expect(screencastWsUrl("https://api-dev.isol8.co/api/v1", null)).toBe(
      "wss://bv-dev.isol8.co/api/v1/browser-view/screencast/ws",
    );
  });
});

// URL bar coercion (ported from the fork's browser-url.ts): a bare host gets
// https://, "host:port" is not mistaken for a scheme, and a non-http(s) scheme
// is rejected so the address bar can't push e.g. javascript:/file: to /navigate.
describe("normalizeBrowserUrlDraft", () => {
  it("prepends https:// to a bare host", () => {
    expect(normalizeBrowserUrlDraft("example.com")).toBe("https://example.com/");
  });
  it("keeps an explicit http(s) scheme", () => {
    expect(normalizeBrowserUrlDraft("http://example.com/x")).toBe("http://example.com/x");
  });
  it("treats host:port as a host, not a scheme", () => {
    expect(normalizeBrowserUrlDraft("localhost:3000")).toBe("https://localhost:3000/");
  });
  it("rejects a non-http(s) scheme and blank input", () => {
    expect(normalizeBrowserUrlDraft("javascript:alert(1)")).toBeNull();
    expect(normalizeBrowserUrlDraft("   ")).toBeNull();
  });
});

// Tab-strip label precedence: title, else URL host, else a fallback.
describe("tabLabel", () => {
  const base = { id: "t1", targetId: "raw", title: "", url: "" };
  it("prefers the title", () => {
    expect(tabLabel({ ...base, title: "My Page", url: "https://x.com" })).toBe("My Page");
  });
  it("falls back to the URL host when untitled", () => {
    expect(tabLabel({ ...base, url: "https://docs.example.com/a/b" })).toBe("docs.example.com");
  });
  it("falls back to 'New tab' for a blank/opaque tab", () => {
    expect(tabLabel(base)).toBe("New tab");
    expect(tabLabel({ ...base, url: "about:blank" })).toBe("New tab");
  });
});

// The coord mapping is the load-bearing part of driving the browser: a click in
// the canvas must land on the same spot in the page. The JPEG's natural pixel
// size cancels out — only the displayed canvas rect + the frame's CSS viewport
// (deviceWidth/Height) matter.
describe("mapPointerToDevice", () => {
  const meta = { deviceWidth: 1280, deviceHeight: 800 };
  // A canvas displayed at 640x400 (half the page's CSS viewport), offset on screen.
  const rect = { left: 100, top: 50, width: 640, height: 400 };

  it("maps the canvas top-left to page (0,0)", () => {
    expect(mapPointerToDevice(100, 50, rect, meta)).toEqual({ x: 0, y: 0 });
  });

  it("maps the canvas center to the page center", () => {
    expect(mapPointerToDevice(100 + 320, 50 + 200, rect, meta)).toEqual({ x: 640, y: 400 });
  });

  it("maps the canvas bottom-right to the page's far corner", () => {
    expect(mapPointerToDevice(100 + 640, 50 + 400, rect, meta)).toEqual({ x: 1280, y: 800 });
  });

  it("clamps a pointer dragged past the canvas edge into the page", () => {
    expect(mapPointerToDevice(9999, 9999, rect, meta)).toEqual({ x: 1280, y: 800 });
    expect(mapPointerToDevice(-9999, -9999, rect, meta)).toEqual({ x: 0, y: 0 });
  });

  it("returns (0,0) for a zero-sized canvas instead of NaN", () => {
    expect(mapPointerToDevice(10, 10, { left: 0, top: 0, width: 0, height: 0 }, meta)).toEqual({
      x: 0,
      y: 0,
    });
  });

  it("scales each axis independently when the canvas aspect ratio differs from the frame (objectFit: fill)", () => {
    // The panel is chosen objectFit:"fill", so the frame stretches to fill the
    // box and each axis maps independently — no letterbox offset even when the
    // canvas is square (640x640) but the page viewport is wide (1280x800). This
    // pins the fill contract: the center of the square canvas is the page center,
    // and the corner is the page's far corner, regardless of aspect mismatch.
    const squareRect = { left: 0, top: 0, width: 640, height: 640 };
    expect(mapPointerToDevice(320, 320, squareRect, meta)).toEqual({ x: 640, y: 400 });
    expect(mapPointerToDevice(640, 640, squareRect, meta)).toEqual({ x: 1280, y: 800 });
  });
});

describe("toCdpMouseEvent", () => {
  it("names the button for press/release and uses 'none' for moves", () => {
    expect(toCdpMouseEvent("mousePressed", { x: 5, y: 6 }, { button: 0, clickCount: 1 })).toMatchObject({
      type: "mousePressed",
      button: "left",
      clickCount: 1,
      x: 5,
      y: 6,
    });
    expect(toCdpMouseEvent("mouseMoved", { x: 5, y: 6 })).toMatchObject({ button: "none" });
  });

  it("maps the right button and carries wheel deltas", () => {
    expect(toCdpMouseEvent("mousePressed", { x: 0, y: 0 }, { button: 2 })).toMatchObject({ button: "right" });
    expect(toCdpMouseEvent("mouseWheel", { x: 0, y: 0 }, { deltaX: 3, deltaY: -4 })).toMatchObject({
      deltaX: 3,
      deltaY: -4,
    });
  });
});

describe("toCdpKeyEvent", () => {
  it("carries text for a printable keydown and none for keyup", () => {
    expect(toCdpKeyEvent("keyDown", { key: "a", code: "KeyA", keyCode: 65 })).toMatchObject({
      type: "keyDown",
      key: "a",
      text: "a",
      windowsVirtualKeyCode: 65,
    });
    expect(toCdpKeyEvent("keyUp", { key: "a", code: "KeyA", keyCode: 65 }).text).toBeUndefined();
  });

  it("gives Enter its carriage return but no text for other named keys", () => {
    expect(toCdpKeyEvent("keyDown", { key: "Enter", code: "Enter", keyCode: 13 }).text).toBe("\r");
    expect(toCdpKeyEvent("keyDown", { key: "ArrowLeft", code: "ArrowLeft", keyCode: 37 }).text).toBeUndefined();
  });
});
