// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

interface ConnectOptions {
  url: string;
  callbacks: { disconnect: () => void; firstFrame: () => void };
}
const dcv = vi.hoisted(() => ({ connects: [] as ConnectOptions[], auths: 0, refuse: null as null | { code: number } }));
vi.mock("dcv", () => ({
  default: {
    authenticate: (_url: string, handlers: { success: (auth: unknown, result: unknown[]) => void; error: (auth: unknown, error: unknown) => void }) => {
      dcv.auths += 1;
      if (dcv.refuse) return handlers.error({}, dcv.refuse);
      handlers.success(null, [{ sessionId: "dcv", authToken: "token" }]);
    },
    connect: async (options: ConnectOptions) => {
      dcv.connects.push(options);
      return { disconnect: vi.fn(), captureClipboardEvents: vi.fn(), enableDisplayQualityUpdates: vi.fn(), setDisplayQuality: vi.fn() };
    },
  },
}));
vi.mock("@/lib/analytics", () => ({ captureException: vi.fn() }));
vi.mock("../BrowserFrameView", () => ({
  BrowserFrameView: ({ empty, onTakeControl, interactive }: { empty: React.ReactNode; onTakeControl: () => void; interactive?: boolean }) => (
    <div data-testid="frames" data-interactive={String(!!interactive)}>{empty}<button type="button" onClick={onTakeControl}>Take live control</button></div>
  ),
}));

import { DirectDcvLiveView } from "../DirectDcvLiveView";

async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  dcv.connects.length = 0;
  dcv.auths = 0;
  dcv.refuse = null;
  window.__lightbulbViewerConnects = {};
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("DirectDcvLiveView connection", () => {
  it("never reconnects for a fresh URL, and reconnects a drop with backoff on a re-signed URL", async () => {
    const resign = vi.fn(async () => "https://signed/fresh");
    const { rerender } = render(<DirectDcvLiveView signedUrl="https://signed/first" sessionId="s1" resign={resign} />);
    await flush();
    expect(dcv.connects.map((c) => c.url)).toEqual(["https://signed/first"]);
    act(() => dcv.connects[0].callbacks.firstFrame());

    // A control change or the 240 s re-sign: a new URL prop, same connection.
    rerender(<DirectDcvLiveView signedUrl="https://signed/second" sessionId="s1" resign={resign} />);
    await flush(60_000);
    expect(dcv.connects).toHaveLength(1);

    // The socket drops: the last frame stays, dimmed, under a quiet chip.
    act(() => dcv.connects[0].callbacks.disconnect());
    expect(screen.getByRole("status")).toHaveTextContent("Reconnecting…");
    expect(screen.queryByText(/disconnected/i)).toBeNull();
    await flush(499);
    expect(dcv.connects).toHaveLength(1);
    await flush(1);
    expect(dcv.connects.map((c) => c.url)).toEqual(["https://signed/first", "https://signed/fresh"]);

    // It drops again before painting: the next wait doubles.
    act(() => dcv.connects[1].callbacks.disconnect());
    await flush(999);
    expect(dcv.connects).toHaveLength(2);
    await flush(1);
    expect(dcv.connects).toHaveLength(3);
    act(() => dcv.connects[2].callbacks.firstFrame());
    expect(screen.queryByRole("status")).toBeNull();
    expect(window.__lightbulbViewerConnects?.s1.map((entry) => entry.reason)).toEqual(["session", "drop", "drop"]);
  });

  it("says it is down, with Retry, only after retrying for 30 s", async () => {
    render(<DirectDcvLiveView signedUrl="https://signed/first" sessionId="s1" />);
    await flush();
    let elapsed = 0;
    while (!screen.queryByRole("alert") && elapsed < 120_000) {
      act(() => dcv.connects.at(-1)!.callbacks.disconnect());
      await flush(1_000);
      elapsed += 1_000;
      await flush(30_000);
      elapsed += 30_000;
    }
    expect(elapsed).toBeGreaterThanOrEqual(30_000);
    expect(screen.getByRole("alert")).toHaveTextContent("Browser viewer disconnected.");
    const before = dcv.connects.length;
    act(() => screen.getByRole("button", { name: "Retry" }).click());
    await flush();
    expect(dcv.connects).toHaveLength(before + 1);
    expect(window.__lightbulbViewerConnects?.s1.at(-1)?.reason).toBe("retry");
  });

  it("says it is being watched elsewhere when the one slot is taken, and never retries on its own", async () => {
    dcv.refuse = { code: 4 }; // NO_SESSIONS: another viewer holds the session's one stream
    const onStatus = vi.fn();
    render(<DirectDcvLiveView signedUrl="https://signed/first" sessionId="s1" onStatus={onStatus} />);
    await flush();
    expect(screen.getByRole("status")).toHaveTextContent("Being watched elsewhere");
    expect(screen.getByTestId("frames")).toHaveAttribute("data-interactive", "true"); // live frames, with input
    expect(onStatus).toHaveBeenLastCalledWith("elsewhere");
    await flush(120_000);
    expect(dcv.auths).toBe(1);

    dcv.refuse = null; // they closed it: the person asks, and it connects
    act(() => screen.getByRole("button", { name: "Take live control" }).click());
    await flush();
    expect(dcv.auths).toBe(2);
    expect(dcv.connects).toHaveLength(1);
  });
});
