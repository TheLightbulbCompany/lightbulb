import { describe, test, expect } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createRef } from "react";
import {
  useOrgPanZoom,
  computeFit,
  panForZoomToward,
} from "@/components/teams/org-chart/useOrgPanZoom";
import { clampZoom, MIN_ZOOM, MAX_ZOOM } from "@/components/teams/org-chart/orgChartLayout";

// ── Pure math helpers ──────────────────────────────────────────────────

describe("clampZoom", () => {
  test("passes through values inside the range", () => {
    expect(clampZoom(1)).toBe(1);
    expect(clampZoom(0.5)).toBe(0.5);
  });
  test("clamps below MIN_ZOOM", () => {
    expect(clampZoom(0.01)).toBe(MIN_ZOOM);
  });
  test("clamps above MAX_ZOOM", () => {
    expect(clampZoom(99)).toBe(MAX_ZOOM);
  });
});

describe("computeFit", () => {
  test("returns identity for zero-sized bounds", () => {
    expect(computeFit({ width: 0, height: 0 }, { width: 800, height: 600 })).toEqual({
      pan: { x: 0, y: 0 },
      zoom: 1,
    });
  });

  test("never scales above 1x for a chart smaller than the viewport", () => {
    const fit = computeFit({ width: 100, height: 100 }, { width: 800, height: 600 });
    expect(fit.zoom).toBe(1);
    // Centered: (800 - 100)/2 = 350 ; (600 - 100)/2 = 250
    expect(fit.pan).toEqual({ x: 350, y: 250 });
  });

  test("scales down + centers a chart larger than the viewport", () => {
    // bounds 2000x1000, viewport 800x600, margin 40.
    // scaleX = (800-40)/2000 = 0.38 ; scaleY = (600-40)/1000 = 0.56 → min = 0.38
    const fit = computeFit({ width: 2000, height: 1000 }, { width: 800, height: 600 });
    expect(fit.zoom).toBeCloseTo(0.38, 5);
    const chartW = 2000 * fit.zoom;
    const chartH = 1000 * fit.zoom;
    expect(fit.pan.x).toBeCloseTo((800 - chartW) / 2, 5);
    expect(fit.pan.y).toBeCloseTo((600 - chartH) / 2, 5);
  });
});

describe("panForZoomToward", () => {
  test("keeps the anchor visually fixed when zooming", () => {
    // Anchor at origin: pan is just scaled.
    //   nextPan = a - (to/from) * (a - pan) = 0 - 2 * (0 - 100) = 200
    // Sanity-check against the render transform (`translate(pan) scale(zoom)`
    // with transform-origin 0 0, so viewport = pan + zoom * content): the
    // content point under the anchor is (0 - 100) / 1 = -100 before and
    // (0 - 200) / 2 = -100 after — i.e. the anchor is fixed.
    const next = panForZoomToward({ x: 100, y: 100 }, 1, 2, { x: 0, y: 0 });
    expect(next).toEqual({ x: 200, y: 200 });
  });

  test("anchor at a point leaves that point invariant under the transform", () => {
    const pan = { x: 50, y: 20 };
    const anchor = { x: 400, y: 300 };
    const fromZoom = 1;
    const toZoom = 1.5;
    const nextPan = panForZoomToward(pan, fromZoom, toZoom, anchor);
    // The content coord under the anchor before == after.
    const beforeContentX = (anchor.x - pan.x) / fromZoom;
    const afterContentX = (anchor.x - nextPan.x) / toZoom;
    expect(afterContentX).toBeCloseTo(beforeContentX, 5);
  });
});

// ── Hook behavior ──────────────────────────────────────────────────────

function setup(bounds = { width: 1000, height: 800 }) {
  const ref = createRef<HTMLDivElement>();
  // jsdom doesn't lay out; stub the container metrics the hook reads.
  const el = document.createElement("div");
  Object.defineProperty(el, "clientWidth", { value: 800, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: 600, configurable: true });
  el.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 800, height: 600, right: 800, bottom: 600, x: 0, y: 0, toJSON() {} }) as DOMRect;
  ref.current = el;
  const { result } = renderHook(() => useOrgPanZoom(ref, bounds));
  return { ref, el, result };
}

describe("useOrgPanZoom", () => {
  test("starts at identity (no pan, 1x, not dragging)", () => {
    const { result } = setup();
    expect(result.current.pan).toEqual({ x: 0, y: 0 });
    expect(result.current.zoom).toBe(1);
    expect(result.current.dragging).toBe(false);
  });

  test("drag-to-pan updates pan by the cursor delta", () => {
    const { result } = setup();
    act(() => {
      result.current.onMouseDown({
        button: 0,
        clientX: 100,
        clientY: 100,
        target: document.createElement("div"),
      } as unknown as React.MouseEvent);
    });
    expect(result.current.dragging).toBe(true);
    act(() => {
      result.current.onMouseMove({ clientX: 130, clientY: 90 } as React.MouseEvent);
    });
    expect(result.current.pan).toEqual({ x: 30, y: -10 });
    act(() => result.current.onMouseUp());
    expect(result.current.dragging).toBe(false);
  });

  test("a mousedown on a card (data-org-card) does not start a pan", () => {
    const { result } = setup();
    const card = document.createElement("a");
    card.setAttribute("data-org-card", "");
    act(() => {
      result.current.onMouseDown({
        button: 0,
        clientX: 0,
        clientY: 0,
        target: card,
      } as unknown as React.MouseEvent);
    });
    expect(result.current.dragging).toBe(false);
  });

  test("zoomIn / zoomOut step the zoom and clamp at the bounds", () => {
    const { result } = setup();
    act(() => result.current.zoomIn());
    expect(result.current.zoom).toBeCloseTo(1.2, 5);
    // Hammer zoomOut past the floor — should clamp at MIN_ZOOM.
    for (let i = 0; i < 30; i++) act(() => result.current.zoomOut());
    expect(result.current.zoom).toBe(MIN_ZOOM);
    // And back up past the ceiling — clamp at MAX_ZOOM.
    for (let i = 0; i < 30; i++) act(() => result.current.zoomIn());
    expect(result.current.zoom).toBe(MAX_ZOOM);
  });

  test("wheel up zooms in toward the cursor (native non-passive listener)", () => {
    const { el, result } = setup();
    // The hook attaches a NON-passive native `wheel` listener on the viewport
    // (so preventDefault() actually fires); dispatch a real cancelable event
    // and assert both the zoom change and that the page-scroll was prevented.
    const evt = new WheelEvent("wheel", {
      deltaY: -1,
      clientX: 400,
      clientY: 300,
      cancelable: true,
      bubbles: true,
    });
    act(() => {
      el.dispatchEvent(evt);
    });
    expect(evt.defaultPrevented).toBe(true);
    expect(result.current.zoom).toBeCloseTo(1.2, 5);
  });

  test("fitToScreen centers + scales using the container metrics", () => {
    // bounds 1000x800, container 800x600, margin 40 →
    // scaleX = 760/1000 = 0.76 ; scaleY = 560/800 = 0.7 → min 0.7
    const { result } = setup({ width: 1000, height: 800 });
    act(() => result.current.fitToScreen());
    expect(result.current.zoom).toBeCloseTo(0.7, 5);
    const chartW = 1000 * 0.7;
    const chartH = 800 * 0.7;
    expect(result.current.pan.x).toBeCloseTo((800 - chartW) / 2, 5);
    expect(result.current.pan.y).toBeCloseTo((600 - chartH) / 2, 5);
  });
});
