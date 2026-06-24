"use client";

// Pan/zoom state machine for the org chart viewport.
//
// Ported from upstream Paperclip's OrgChart pan/zoom behavior
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI):
//   - drag-to-pan (mouse) with a card-click guard
//   - wheel-to-zoom anchored on the cursor
//   - zoom-toward-point for the +/- buttons (anchored on viewport center)
//   - fit-to-screen that centers the chart and clamps to 1x
//   - clamped zoom bounds (MIN_ZOOM..MAX_ZOOM)
//
// Touch pinch/pan is intentionally deferred — the chart lives on the
// desktop `/teams` surface; this keeps the hook (and its tests) focused on
// the mouse + button + wheel paths that surface needs. The transform math
// is identical to upstream so a later touch port can reuse it.

import { useCallback, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { MIN_ZOOM, MAX_ZOOM, clampZoom } from "./orgChartLayout";

export { MIN_ZOOM, MAX_ZOOM };

export interface Point {
  x: number;
  y: number;
}

export interface ChartBounds {
  width: number;
  height: number;
}

export interface OrgPanZoom {
  /** Current pan offset (px) applied as `translate(pan.x, pan.y)`. */
  pan: Point;
  /** Current zoom factor applied as `scale(zoom)`. */
  zoom: number;
  /** True while the user is dragging to pan (drives the grab/grabbing cursor). */
  dragging: boolean;
  /** Mouse handlers for the viewport element. */
  onMouseDown: (e: React.MouseEvent) => void;
  onMouseMove: (e: React.MouseEvent) => void;
  onMouseUp: () => void;
  /** Zoom in/out by a fixed step, anchored on the viewport center. */
  zoomIn: () => void;
  zoomOut: () => void;
  /** Scale + center the whole chart so it fits the viewport (never above 1x). */
  fitToScreen: () => void;
}

/**
 * Compute the pan+zoom that fits `bounds` inside a `width`x`height` viewport
 * with `margin` px of breathing room, centered. Never scales above 1x (we
 * don't blow small charts up). Pure — exported so tests can assert the math
 * without a DOM.
 */
export function computeFit(
  bounds: ChartBounds,
  viewport: { width: number; height: number },
  margin = 40,
): { pan: Point; zoom: number } {
  // Degenerate inputs (empty chart, or a not-yet-laid-out viewport reporting
  // 0×0 — common in jsdom and on the first paint before layout) → identity,
  // so we never apply a negative or NaN scale.
  if (
    bounds.width <= 0 ||
    bounds.height <= 0 ||
    viewport.width <= margin ||
    viewport.height <= margin
  ) {
    return { pan: { x: 0, y: 0 }, zoom: 1 };
  }
  const scaleX = (viewport.width - margin) / bounds.width;
  const scaleY = (viewport.height - margin) / bounds.height;
  const fitZoom = Math.min(scaleX, scaleY, 1);
  const chartW = bounds.width * fitZoom;
  const chartH = bounds.height * fitZoom;
  return {
    pan: {
      x: (viewport.width - chartW) / 2,
      y: (viewport.height - chartH) / 2,
    },
    zoom: fitZoom,
  };
}

/**
 * Compute the pan that keeps `anchor` (a viewport-local point) visually fixed
 * while zooming from `fromZoom` to `toZoom`. Pure — the cursor/center anchor
 * math both wheel and button zoom share. Mirrors upstream's
 * `zoomTowardPoint`.
 */
export function panForZoomToward(
  pan: Point,
  fromZoom: number,
  toZoom: number,
  anchor: Point,
): Point {
  const scale = toZoom / fromZoom;
  return {
    x: anchor.x - scale * (anchor.x - pan.x),
    y: anchor.y - scale * (anchor.y - pan.y),
  };
}

const ZOOM_STEP_IN = 1.2;
const ZOOM_STEP_OUT = 0.8;

export function useOrgPanZoom(
  containerRef: RefObject<HTMLDivElement | null>,
  bounds: ChartBounds,
): OrgPanZoom {
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [dragging, setDragging] = useState(false);
  const dragStart = useRef({ x: 0, y: 0, panX: 0, panY: 0 });

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      // A click on a card should navigate, not start a pan.
      const target = e.target as HTMLElement;
      if (target.closest("[data-org-card]")) return;
      setDragging(true);
      dragStart.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y };
    },
    [pan],
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!dragging) return;
      const dx = e.clientX - dragStart.current.x;
      const dy = e.clientY - dragStart.current.y;
      setPan({ x: dragStart.current.panX + dx, y: dragStart.current.panY + dy });
    },
    [dragging],
  );

  const onMouseUp = useCallback(() => setDragging(false), []);

  const zoomToward = useCallback(
    (nextZoomRaw: number, anchor: Point) => {
      const nextZoom = clampZoom(nextZoomRaw);
      setPan((prev) => panForZoomToward(prev, zoom, nextZoom, anchor));
      setZoom(nextZoom);
    },
    [zoom],
  );

  // Wheel-to-zoom must call preventDefault(), but React's synthetic onWheel is
  // bound through a (Chrome-default) PASSIVE root listener — calling
  // preventDefault() there warns and no-ops, letting the page scroll under the
  // zoom. Attach a NON-passive native `wheel` listener on the viewport instead.
  // Kept in a ref so the effect can stay mounted across renders without
  // re-subscribing on every zoom change.
  const wheelHandlerRef = useRef<(e: WheelEvent) => void>(() => {});
  // Refresh the ref to the latest closure each render (zoom/zoomToward change)
  // inside an effect — writing a ref's .current during render is disallowed
  // by react-hooks rules and is not safe under concurrent rendering.
  useEffect(() => {
    wheelHandlerRef.current = (e: WheelEvent) => {
      const container = containerRef.current;
      if (!container) return;
      e.preventDefault();
      const rect = container.getBoundingClientRect();
      const anchor = { x: e.clientX - rect.left, y: e.clientY - rect.top };
      const factor = e.deltaY < 0 ? ZOOM_STEP_IN : ZOOM_STEP_OUT;
      zoomToward(zoom * factor, anchor);
    };
  });

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const listener = (e: WheelEvent) => wheelHandlerRef.current(e);
    container.addEventListener("wheel", listener, { passive: false });
    return () => container.removeEventListener("wheel", listener);
  }, [containerRef]);

  const zoomByStep = useCallback(
    (factor: number) => {
      const container = containerRef.current;
      const anchor = container
        ? { x: container.clientWidth / 2, y: container.clientHeight / 2 }
        : { x: 0, y: 0 };
      zoomToward(zoom * factor, anchor);
    },
    [containerRef, zoom, zoomToward],
  );

  const zoomIn = useCallback(() => zoomByStep(ZOOM_STEP_IN), [zoomByStep]);
  const zoomOut = useCallback(() => zoomByStep(ZOOM_STEP_OUT), [zoomByStep]);

  const fitToScreen = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const fit = computeFit(bounds, {
      width: container.clientWidth,
      height: container.clientHeight,
    });
    setPan(fit.pan);
    setZoom(fit.zoom);
  }, [containerRef, bounds]);

  return {
    pan,
    zoom,
    dragging,
    onMouseDown,
    onMouseMove,
    onMouseUp,
    zoomIn,
    zoomOut,
    fitToScreen,
  };
}
