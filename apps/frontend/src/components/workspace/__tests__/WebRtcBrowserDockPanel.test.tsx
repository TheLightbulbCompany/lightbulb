import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { IDockviewPanelProps } from "dockview-react";
import React from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { callRpcMock, getTokenMock } = vi.hoisted(() => ({
  callRpcMock: vi.fn(),
  getTokenMock: vi.fn(),
}));

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ getToken: getTokenMock }) }));
vi.mock("@/hooks/useGatewayRpc", () => ({ useGatewayRpcMutation: () => callRpcMock }));
vi.mock("@/lib/api", () => ({ BACKEND_URL: "https://api-dev.isol8.co/api/v1" }));

class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  constructor(readonly callback: ResizeObserverCallback) {
    MockResizeObserver.instances.push(this);
  }
  observe(): void {}
  disconnect = vi.fn();
}

import { WebRtcBrowserDockPanel } from "../panels/WebRtcBrowserDockPanel";

const SESSION = { iframe_url: "/api/v1/browser-view/webrtc/" };

function response(body: unknown, ok = true): Response {
  return { ok, json: vi.fn().mockResolvedValue(body) } as unknown as Response;
}

function mount() {
  return render(<WebRtcBrowserDockPanel {...({} as IDockviewPanelProps)} />);
}

beforeEach(() => {
  getTokenMock.mockResolvedValue("clerk-token");
  callRpcMock.mockResolvedValue({});
  MockResizeObserver.instances = [];
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("WebRtcBrowserDockPanel", () => {
  it("starts Chrome without any other browser lifecycle RPC", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(SESSION)));
    mount();
    await screen.findByTitle("Your shared Chrome browser");
    expect(callRpcMock).toHaveBeenCalledWith("browser.request", { method: "POST", path: "/start" });
    expect(callRpcMock).toHaveBeenCalledTimes(1);
  });

  it("bootstraps with bearer credentials before it mounts the iframe", async () => {
    let resolveSession!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(new Promise<Response>((resolve) => { resolveSession = resolve; })));
    mount();
    expect(screen.queryByTitle("Your shared Chrome browser")).toBeNull();
    await waitFor(() => expect(getTokenMock).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledWith("https://bv-dev.isol8.co/api/v1/browser-view/session", {
      method: "POST",
      headers: { Authorization: "Bearer clerk-token" },
      credentials: "include",
      signal: expect.any(AbortSignal),
    });
    await act(async () => resolveSession(response(SESSION)));
    expect((await screen.findByTitle("Your shared Chrome browser")).src).toBe(
      "https://bv-dev.isol8.co/api/v1/browser-view/webrtc/?cast=1&volume=1",
    );
  });

  it("shows Retry after bootstrap failure and makes a fresh attempt", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response({}, false)).mockResolvedValueOnce(response(SESSION));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await screen.findByTitle("Your shared Chrome browser");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(callRpcMock).toHaveBeenCalledTimes(2);
  });

  it("uses the Neko iframe permissions and waits for Neko to report watching", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(SESSION));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    expect(frame).toHaveAttribute("allow", "autoplay; clipboard-read; clipboard-write");
    expect(frame).toHaveAttribute("allowfullscreen");
    fireEvent.load(frame);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "https://bv-dev.isol8.co/api/v1/browser-view/webrtc/api/whoami",
      expect.objectContaining({ credentials: "include" }),
    ));
    expect(screen.getByText(/Connecting to your browser/)).toBeInTheDocument();
  });

  it("clears the veil only after state.is_watching is true", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response({ state: { is_watching: false } }))
      .mockResolvedValueOnce(response({ state: { is_watching: true } }));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    fireEvent.load(frame);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("keeps polling through a transient whoami failure until Neko is watching", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response({}, false))
      .mockResolvedValueOnce(response({ state: { is_watching: true } }));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    fireEvent.load(frame);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("replaces readiness polling when the iframe reloads", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValue(response({ state: { is_watching: false } }));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    fireEvent.load(frame);
    fireEvent.load(frame);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    const whoamiCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/whoami"));
    expect(whoamiCalls).toHaveLength(3);
  });

  it("returns to the connecting veil when a connected iframe reloads", async () => {
    const watching = [true, false, true];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (url.endsWith("/session")) return Promise.resolve(response(SESSION));
      if (url.endsWith("/api/whoami")) return Promise.resolve(response({ state: { is_watching: watching.shift() } }));
      return Promise.resolve(response({}));
    }));
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    fireEvent.load(frame);
    await waitFor(() => expect(screen.queryByText(/Connecting to your browser/)).toBeNull());
    vi.useFakeTimers();
    fireEvent.load(frame);
    expect(screen.getByText(/Connecting to your browser/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(screen.queryByText(/Connecting to your browser/)).toBeNull();
  });

  it("does not overlap readiness requests while one is in flight", async () => {
    let resolveWhoami!: (value: Response) => void;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/session")) return Promise.resolve(response(SESSION));
      return new Promise<Response>((resolve) => { resolveWhoami = resolve; });
    });
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    fireEvent.load(frame);
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    const whoamiCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/whoami"));
    expect(whoamiCalls).toHaveLength(1);
    await act(async () => resolveWhoami(response({ state: { is_watching: true } })));
  });

  it("ignores a late whoami result from an earlier iframe load", async () => {
    let resolveOld!: (value: Response) => void;
    let resolveNew!: (value: Response) => void;
    let whoamiRequest = 0;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (url.endsWith("/session")) return Promise.resolve(response(SESSION));
      whoamiRequest += 1;
      return new Promise<Response>((resolve) => {
        if (whoamiRequest === 1) resolveOld = resolve;
        else resolveNew = resolve;
      });
    }));
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    fireEvent.load(frame);
    fireEvent.load(frame);
    await waitFor(() => expect(whoamiRequest).toBe(2));
    await act(async () => resolveOld(response({ state: { is_watching: true } })));
    expect(screen.getByText(/Connecting to your browser/)).toBeInTheDocument();
    await act(async () => resolveNew(response({ state: { is_watching: true } })));
  });

  it("aborts an in-flight whoami request at the readiness timeout", async () => {
    let signal: AbortSignal | undefined;
    let whoamiCalls = 0;
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit) => {
      if (url.endsWith("/session")) return Promise.resolve(response(SESSION));
      whoamiCalls += 1;
      signal = init.signal;
      return new Promise<Response>(() => {});
    }));
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    fireEvent.load(frame);
    expect(signal).toBeDefined();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(signal?.aborted).toBe(true);
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    expect(whoamiCalls).toBe(1);
  });

  it("keeps Retry after an iframe error even if the cancelled whoami later resolves true", async () => {
    let resolveWhoami!: (value: Response) => void;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      if (url.endsWith("/session")) return Promise.resolve(response(SESSION));
      return new Promise<Response>((resolve) => { resolveWhoami = resolve; });
    }));
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    fireEvent.load(frame);
    await act(async () => fireEvent(frame, new Event("error", { bubbles: true })));
    await act(async () => resolveWhoami(response({ state: { is_watching: true } })));
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("aborts a hanging auth refresh before Retry starts fresh bootstrap", async () => {
    let refreshSignal: AbortSignal | undefined;
    let sessionCount = 0;
    let retryStartedAfterAbort = false;
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit) => {
      if (url.endsWith("/api/whoami")) return Promise.resolve(response({ state: { is_watching: true } }));
      if (!url.endsWith("/session")) return Promise.resolve(response({}));
      sessionCount += 1;
      if (sessionCount === 1) return Promise.resolve(response(SESSION));
      if (sessionCount === 2) {
        refreshSignal = init.signal;
        return new Promise<Response>(() => {});
      }
      retryStartedAfterAbort = refreshSignal?.aborted === true;
      return Promise.resolve(response(SESSION));
    }));
    vi.useFakeTimers();
    mount();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const frame = screen.getByTitle("Your shared Chrome browser");
    fireEvent.load(frame);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByText(/Connecting to your browser/)).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
    expect(refreshSignal).toBeDefined();
    await act(async () => fireEvent(frame, new Event("error", { bubbles: true })));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await Promise.resolve();
    });
    expect(retryStartedAfterAbort).toBe(true);
  });

  it("refreshes auth every 45 seconds without dropping a watched stream on a transient failure", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response({ state: { is_watching: true } }))
      .mockResolvedValueOnce(response({}, false));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    fireEvent.load(frame);
    await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
    expect(screen.queryByText(/Connecting to your browser/)).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "https://bv-dev.isol8.co/api/v1/browser-view/session",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("posts one debounced normalized screen size after connection and skips duplicates", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response({ state: { is_watching: true } }))
      .mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    vi.useFakeTimers();
    const wrapper = frame.parentElement as HTMLDivElement;
    wrapper.getBoundingClientRect = () => ({ width: 811.9, height: 622.9 }) as DOMRect;
    fireEvent.load(frame);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const observer = MockResizeObserver.instances[0];
    act(() => observer.callback([], observer as unknown as ResizeObserver));
    act(() => observer.callback([], observer as unknown as ResizeObserver));
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    const screenCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/room/screen"));
    expect(screenCalls).toHaveLength(1);
    expect(JSON.parse(screenCalls[0][1].body)).toEqual({ width: 808, height: 616, rate: 24 });
  });

  it("retries an unchanged screen size after a failed resize request", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response({ state: { is_watching: true } }))
      .mockResolvedValueOnce(response({}, false))
      .mockResolvedValueOnce(response({}));
    vi.stubGlobal("fetch", fetchMock);
    mount();
    const frame = await screen.findByTitle("Your shared Chrome browser");
    const wrapper = frame.parentElement as HTMLDivElement;
    wrapper.getBoundingClientRect = () => ({ width: 640, height: 400 }) as DOMRect;
    fireEvent.load(frame);
    await waitFor(() => expect(MockResizeObserver.instances).toHaveLength(1));
    vi.useFakeTimers();
    const observer = MockResizeObserver.instances[0];
    act(() => observer.callback([], observer as unknown as ResizeObserver));
    await act(async () => { await vi.advanceTimersByTimeAsync(300); });
    const screenCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/api/room/screen"));
    expect(screenCalls).toHaveLength(2);
  });

  it("aborts outstanding work and disconnects its observer on retry and unmount", async () => {
    let signal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn().mockImplementation((_url, init) => {
      signal = init.signal;
      return new Promise<Response>(() => {});
    }));
    const { unmount } = mount();
    await waitFor(() => expect(signal).toBeDefined());
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it("disconnects the resize observer after a connected panel unmounts", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(SESSION))
      .mockResolvedValueOnce(response({ state: { is_watching: true } }))
      .mockResolvedValue(response({}));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = mount();
    fireEvent.load(await screen.findByTitle("Your shared Chrome browser"));
    await waitFor(() => expect(MockResizeObserver.instances).toHaveLength(1));
    const observer = MockResizeObserver.instances[0];
    unmount();
    expect(observer.disconnect).toHaveBeenCalledTimes(1);
  });

  it("contains no legacy visual transport or browser-control surface", () => {
    const source = readFileSync(join(__dirname, "../panels/WebRtcBrowserDockPanel.tsx"), "utf8");
    expect(source).not.toMatch(/noVNC|screencast|\/tabs|navigate|Address bar/i);
  });
});
