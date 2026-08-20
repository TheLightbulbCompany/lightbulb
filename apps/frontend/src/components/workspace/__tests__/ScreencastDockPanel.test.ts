import { describe, it, expect } from "vitest";
import { screencastWsUrl } from "../panels/browserViewUrl";
import { mapPointerToDevice, toCdpMouseEvent, toCdpKeyEvent } from "../panels/screencastInput";

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
