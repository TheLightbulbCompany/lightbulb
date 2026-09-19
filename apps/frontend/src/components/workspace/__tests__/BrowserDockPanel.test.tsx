// @vitest-environment jsdom
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

const get = vi.fn();
const state = vi.hoisted(() => ({
  host: "agentcore" as "agentcore" | undefined,
  participants: [] as { agentActivity: string }[],
  runs: [] as { state: string }[],
}));

vi.mock("@lightbulb/shared", () => ({ useRuntimeHost: () => state.host }));
// Identity-stable, like the real `useApi()` (a `useMemo` over the Clerk token
// getter) — a fresh object per render would re-fire the panel's mount fetch.
const apiClient = { get };
vi.mock("@/lib/api", () => ({ useApi: () => apiClient }));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({ roomProps: { runs: state.runs }, directoryProps: { participants: state.participants } }),
}));
vi.mock("bedrock-agentcore/browser/live-view", () => ({
  BrowserLiveView: ({ signedUrl, remoteWidth, remoteHeight }: {
    signedUrl: string;
    remoteWidth?: number;
    remoteHeight?: number;
  }) => {
    const [initialUrl] = useState(signedUrl);
    return <div
      data-testid="dcv-live-view"
      data-signed-url={signedUrl}
      data-initial-url={initialUrl}
      data-remote-width={remoteWidth}
      data-remote-height={remoteHeight}
    />;
  },
}));

import { BrowserDockPanel } from "../panels/BrowserDockPanel";

// RTL's waitFor/findBy* only detect JEST fake timers (`typeof jest !== 'undefined'`
// in @testing-library/dom 10.4), so under vitest's they hang on a faked
// setInterval. Every wait here therefore drives the clock explicitly.
async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const RUNNING = [{ state: "running" }];

beforeEach(() => {
  vi.useFakeTimers();
  get.mockReset();
  state.host = "agentcore";
  state.runs = [];
  state.participants = [];
});

describe("BrowserDockPanel", () => {
  it("polls every 5s while a run is active, and stops when it ends", async () => {
    // Browser sessions live 4-15s (design §2g / ruling R14). A 240s poll would
    // catch one ~6% of the time, so the panel would read empty while the agent
    // was visibly browsing. 240s is the RE-MINT bound for a URL already on
    // screen, not a poll interval.
    get.mockResolvedValue({ active: false, url: null, expiresAt: null, remoteWidth: 1920, remoteHeight: 1080 });
    state.runs = RUNNING;

    const view = render(<BrowserDockPanel />);
    await flush(15_000);
    expect(get).toHaveBeenCalledTimes(4); // mount + 3
    expect(get).toHaveBeenCalledWith("/browser/live-view");

    state.runs = [];
    view.rerender(<BrowserDockPanel />);
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(5); // final check on run end, then idle
  });

  it("renders the live view with the session's own dimensions and re-mints before the URL expires", async () => {
    get.mockResolvedValue({
      active: true,
      url: "https://signed",
      expiresAt: "2026-09-19T00:05:00Z",
      remoteWidth: 1280,
      remoteHeight: 720,
    });

    render(<BrowserDockPanel />);
    await flush();
    const live = screen.getByTestId("dcv-live-view");
    expect(live).toHaveAttribute("data-signed-url", "https://signed");
    // The SESSION's viewport, not a constant — a mismatch crops or letterboxes.
    expect(live).toHaveAttribute("data-remote-width", "1280");
    expect(live).toHaveAttribute("data-remote-height", "720");

    expect(get).toHaveBeenCalledTimes(1);
    await flush(240_000); // AWS caps the presigned URL at 300s
    expect(get.mock.calls.length).toBeGreaterThan(1);
  });

  it("shows the empty state and starts nothing when no session is live", async () => {
    // A session costs $0.1273/hour, so opening the panel never POSTs one.
    get.mockResolvedValue({ active: false, url: null, expiresAt: null, remoteWidth: 1920, remoteHeight: 1080 });

    render(<BrowserDockPanel />);
    await flush();

    expect(screen.getByText(/no browser session running/i)).toBeInTheDocument();
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
  });

  it("keeps a session connected between polls and reauthenticates for a new session", async () => {
    state.runs = RUNNING;
    const url = "https://browser.test/sessions/one/live-view?signature=first";
    get.mockResolvedValue({ active: true, url });
    render(<BrowserDockPanel />);
    await flush();
    get.mockResolvedValue({ active: true, url: url.replace("first", "second") });
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", url);
    await flush(235_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-initial-url", url.replace("first", "second"));
    const nextUrl = url.replace("/one/", "/two/");
    get.mockResolvedValue({ active: true, url: nextUrl });
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-initial-url", nextUrl);
  });

  it("polls for an autonomous workspace run outside the selected room", async () => {
    state.participants = [{ agentActivity: "running" }];
    get.mockResolvedValue({ active: false });
    render(<BrowserDockPanel />);
    await flush(10_000);
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("keeps discovering the end of a session found outside known runs", async () => {
    get.mockResolvedValue({ active: true, url: "https://browser.test/sessions/other/live-view" });
    render(<BrowserDockPanel />);
    await flush();
    get.mockResolvedValue({ active: false });
    await flush(5_000);
    expect(screen.getByText(/no browser session running/i)).toBeInTheDocument();
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("offers no take-control affordance", async () => {
    // Take-control destroys the agent's automation session
    // (aws/bedrock-agentcore-sdk-python#518), so the panel is watch-only.
    get.mockResolvedValue({ active: true, url: "https://signed", expiresAt: null, remoteWidth: 1280, remoteHeight: 720 });

    render(<BrowserDockPanel />);
    await flush();

    expect(screen.queryByRole("button", { name: /take control/i })).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing and fires no request until the runtime host is known", async () => {
    state.host = undefined;
    state.runs = RUNNING;

    const { container } = render(<BrowserDockPanel />);
    await flush(30_000);

    expect(get).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });
});
