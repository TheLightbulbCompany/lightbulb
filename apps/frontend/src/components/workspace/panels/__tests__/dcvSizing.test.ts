import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MAX_LAYOUT, MIN_LAYOUT, fitDcvDisplay, layoutRequester, panelLayout } from "../dcvSizing";

describe("fitDcvDisplay", () => {
  it("fills the width of a taller panel, top-aligned (no band above)", () => {
    expect(fitDcvDisplay(920, 600, 1920, 1080)).toEqual({
      scale: 920 / 1920,
      offsetX: 0,
      offsetY: 0,
    });
  });

  it("centers a narrow remote display within a wide panel", () => {
    expect(fitDcvDisplay(1200, 600, 800, 1200)).toEqual({
      scale: 0.5,
      offsetX: 400,
      offsetY: 0,
    });
  });

  it("never renders a 1920-wide remote at 1:1 in a narrower panel", () => {
    const fit = fitDcvDisplay(1540, 800, 1920, 1080);
    expect(1920 * fit.scale).toBeLessThanOrEqual(1540);
    expect(1080 * fit.scale).toBeLessThanOrEqual(800);
  });

  it("fits the width when the engine reports no height for the container", () => {
    expect(fitDcvDisplay(960, 0, 1920, 1080)).toEqual({ scale: 0.5, offsetX: 0, offsetY: 0 });
  });
});

describe("panelLayout", () => {
  it("asks for the panel's own size on the server's 8-pixel grid", () => {
    expect(panelLayout(1136, 651)).toEqual({ width: 1136, height: 648 });
    expect(panelLayout(1560.6, 900.4)).toEqual({ width: 1560, height: 896 });
  });

  it("scales an out-of-range panel as a whole, keeping its aspect", () => {
    expect(panelLayout(2560, 1440)).toEqual(MAX_LAYOUT);
    expect(panelLayout(2560, 1000)).toEqual({ width: 1920, height: 744 });
    expect(panelLayout(320, 240)).toEqual(MIN_LAYOUT);
    expect(panelLayout(0, 900)).toBeNull();
  });
});

describe("layoutRequester", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const setup = (initial = { width: 1920, height: 1080 }) => {
    const state = { busy: false, current: initial };
    const request = vi.fn((size: { width: number; height: number }) => { state.current = size; });
    const requester = layoutRequester({ request, current: () => state.current, busy: () => state.busy, delayMs: 400 });
    return { state, request, requester };
  };

  it("sends one request for the last size of a resize burst", () => {
    const { request, requester } = setup();
    requester.schedule(1000, 700);
    vi.advanceTimersByTime(200);
    requester.schedule(1200, 800);
    vi.advanceTimersByTime(200);
    requester.schedule(1560, 900);
    expect(request).not.toHaveBeenCalled();
    vi.advanceTimersByTime(400);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith({ width: 1560, height: 896 });
  });

  it("drops the request while the browser is changing control", () => {
    const { state, request, requester } = setup();
    state.busy = true;
    requester.schedule(1560, 900);
    vi.advanceTimersByTime(2000);
    expect(request).not.toHaveBeenCalled();
    state.busy = false;
    requester.schedule(1560, 900); // the viewer re-schedules when busy clears
    vi.advanceTimersByTime(400);
    expect(request).toHaveBeenCalledWith({ width: 1560, height: 896 });
  });

  it("skips a request for the size the remote already has", () => {
    const { request, requester } = setup({ width: 1136, height: 648 });
    requester.schedule(1136, 651);
    vi.advanceTimersByTime(400);
    expect(request).not.toHaveBeenCalled();
  });

  it("cancel drops a pending request", () => {
    const { request, requester } = setup();
    requester.schedule(1560, 900);
    requester.cancel();
    vi.advanceTimersByTime(400);
    expect(request).not.toHaveBeenCalled();
  });
});
