// @vitest-environment jsdom
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const get = vi.fn();
const post = vi.fn();
const state = vi.hoisted(() => ({
  scope: "org-one",
  host: true,
  participants: [] as { agentActivity: string }[],
  runs: [] as { state: string }[],
}));

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: state.scope, userId: "human" }) }));
vi.mock("@lightbulb/shared", () => ({ useStatusLoaded: () => state.host }));
// Identity-stable, like the real `useApi()` (a `useMemo` over the Clerk token
// getter) — a fresh object per render would re-fire the panel's mount fetch.
const apiClient = { get, post };
vi.mock("@/lib/api", () => ({
  ApiError: class extends Error { constructor(public status: number) { super(); } },
  useApi: () => apiClient,
}));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspaceOptional: () => ({ roomProps: { runs: state.runs }, directoryProps: { participants: state.participants } }),
}));
vi.mock("../panels/DirectDcvLiveView", () => ({
  DirectDcvLiveView: ({ signedUrl, remoteWidth, remoteHeight }: {
    signedUrl: string;
    remoteWidth?: number;
    remoteHeight?: number;
  }) => {
    const [initialUrl, setInitialUrl] = useState(signedUrl);
    useEffect(() => setInitialUrl(signedUrl), [signedUrl]);
    return <div
      data-testid="dcv-live-view"
      data-signed-url={signedUrl}
      data-initial-url={initialUrl}
      data-remote-width={remoteWidth}
      data-remote-height={remoteHeight}
    />;
  },
}));

import { ApiError } from "@/lib/api";
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
  post.mockReset();
  state.host = true;
  state.scope = "org-one";
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
    expect(get).toHaveBeenCalledWith("/browser/live-view", expect.objectContaining({ signal: expect.any(AbortSignal) }));

    state.runs = [];
    view.rerender(<BrowserDockPanel />);
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(5); // final check on run end, then idle
  });

  it("renders the live view with the session's own dimensions and re-mints before the URL expires", async () => {
    get.mockResolvedValue({
      active: true,
      url: "https://signed",
      sessionId: "s1",
      generation: "g1",
      mode: "agent",
      canControl: true,
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

    expect(screen.getByText(/no shared browser session is running/i)).toBeInTheDocument();
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
  });

  it("explicitly starts an inactive shared browser and never starts on mount", async () => {
    get.mockResolvedValue({ active: false, mode: "idle" });
    render(<BrowserDockPanel />);
    await flush();
    expect(post).not.toHaveBeenCalled();
    post.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 1, mode: "human", isController: true, canControl: true });
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 1, mode: "human", isController: true, canControl: true });
    await act(async () => { screen.getByRole("button", { name: /start browser/i }).click(); });
    expect(post).toHaveBeenCalledWith("/browser/start", {});
    expect(screen.getByRole("button", { name: /done & stop/i })).toBeInTheDocument();
  });

  it("takes an idle session for the viewer automatically", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle", canControl: true });
    post.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "human", isController: true, canControl: true });
    render(<BrowserDockPanel />);
    await flush();
    expect(post).toHaveBeenCalledWith("/browser/control", { action: "take", generation: "g1" });
    expect(screen.getByRole("button", { name: /done & stop/i })).toBeInTheDocument();
  });

  it("merges fresh control metadata while retaining the current signed URL", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://signed/one", sessionId: "s1", generation: 1, mode: "agent", canControl: true })
      .mockResolvedValue({ active: true, url: "https://signed/one?new", sessionId: "s1", generation: 2, mode: "human", isController: false, canControl: true });
    render(<BrowserDockPanel />);
    await flush();
    await flush(10_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", "https://signed/one");
    expect(screen.getByRole("button", { name: /take control/i })).toBeInTheDocument();
  });

  it("does not let an older poll overwrite a newer controller mutation", async () => {
    let resolvePoll!: (value: unknown) => void;
    get.mockResolvedValueOnce({ active: true, url: "https://signed/one", sessionId: "s1", generation: 1, mode: "agent", canControl: true })
      .mockReturnValueOnce(new Promise((resolve) => { resolvePoll = resolve; }));
    render(<BrowserDockPanel />);
    await flush();
    post.mockResolvedValue({ active: true, url: "https://signed/one", sessionId: "s1", generation: 2, mode: "human", isController: true, canControl: true });
    await flush(5_000);
    await act(async () => { screen.getByRole("button", { name: /take control/i }).click(); });
    resolvePoll({ active: true, url: "https://signed/one", sessionId: "s1", generation: 1, mode: "agent", canControl: true });
    get.mockResolvedValue({ active: true, url: "https://signed/one", sessionId: "s1", generation: 2, mode: "human", isController: true, canControl: true });
    await flush();
    expect(screen.getByRole("button", { name: /done & stop/i })).toBeInTheDocument();
  });

  it("offers restart recovery for an errored browser and does not offer take control", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 4, mode: "error", error: "session unavailable" });
    render(<BrowserDockPanel />);
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("session unavailable");
    expect(screen.queryByRole("button", { name: /take control/i })).toBeNull();
    post.mockResolvedValue({ active: true, url: "https://signed/new", sessionId: "s2", generation: 1, mode: "human", isController: true, canControl: true });
    await act(async () => { screen.getByRole("button", { name: /restart browser/i }).click(); });
    expect(post).toHaveBeenCalledWith("/browser/start", {});
  });

  it("keeps a legacy session visible without starting a second browser", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", generation: null });
    render(<BrowserDockPanel />);
    await flush();
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    expect(screen.getByText(/after this existing session finishes/i)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it("navigates controller input with a normalized absolute URL", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "human", isController: true, canControl: true });
    post.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "human", isController: true, canControl: true });
    render(<BrowserDockPanel />);
    await flush();
    fireEvent.change(screen.getByRole("textbox", { name: /website address/i }), { target: { value: "example.com" } });
    await act(async () => { fireEvent.submit(screen.getByRole("form", { name: /navigate shared browser/i })); });
    await flush();
    expect(post).toHaveBeenCalledWith("/browser/navigate", { generation: "g1", url: "https://example.com/" });
  });

  it("shows validation errors and makes no request for unsupported navigation URLs", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "human", isController: true, canControl: true });
    render(<BrowserDockPanel />);
    await flush();
    fireEvent.change(screen.getByRole("textbox", { name: /website address/i }), { target: { value: "javascript:alert(1)" } });
    await act(async () => { fireEvent.submit(screen.getByRole("form", { name: /navigate shared browser/i })); });
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent(/http/i);
    expect(post).not.toHaveBeenCalled();
  });

  it("does not expose navigation controls to a noncontroller", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "human", isController: false, canControl: false });
    render(<BrowserDockPanel />);
    await flush();
    expect(screen.queryByRole("textbox", { name: /website address/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Go" })).toBeNull();
  });

  it("keeps a session connected between polls and reauthenticates for a new session", async () => {
    state.runs = RUNNING;
    const url = "https://browser.test/sessions/one/live-view?signature=first";
    get.mockResolvedValue({ active: true, url, sessionId: "s1", generation: "g1", mode: "agent", canControl: true });
    render(<BrowserDockPanel />);
    await flush();
    get.mockResolvedValue({ active: true, url: url.replace("first", "second"), sessionId: "s1", generation: "g1", mode: "human", isController: false, canControl: true });
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", url);
    await flush(235_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-initial-url", url.replace("first", "second"));
    const nextUrl = url.replace("/one/", "/two/");
    get.mockResolvedValue({ active: true, url: nextUrl, sessionId: "s2", generation: "g2", mode: "agent", canControl: true });
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
    get.mockResolvedValue({ active: true, url: "https://browser.test/sessions/other/live-view", sessionId: "s2", generation: "g2", mode: "agent", canControl: true });
    render(<BrowserDockPanel />);
    await flush();
    get.mockResolvedValue({ active: false });
    await flush(5_000);
    expect(screen.getByText(/no shared browser session is running/i)).toBeInTheDocument();
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("retries a transient failure for a known session without losing the viewer", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "s1", generation: "g1", mode: "agent", canControl: true })
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ active: true, url: "https://browser.test/sessions/two/live-view", sessionId: "s2", generation: "g2", mode: "agent", canControl: true });
    render(<BrowserDockPanel />);
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    await flush(5_000);
    expect(get).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", "https://browser.test/sessions/two/live-view");
  });

  it("bounds a hung request and aborts the next one on unmount", async () => {
    state.runs = RUNNING;
    get.mockImplementation((_path, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("aborted")));
    }));
    const panel = render(<BrowserDockPanel />);
    await flush(15_000);
    expect(get).toHaveBeenCalledTimes(2);
    expect(get.mock.calls[0][1].signal.aborted).toBe(true);
    panel.unmount();
    expect(get.mock.calls[1][1].signal.aborted).toBe(true);
  });

  it("clears the viewer and stops polling when access is revoked", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "s1", generation: "g1", mode: "agent", canControl: true })
      .mockRejectedValue(new ApiError(403, {}));
    render(<BrowserDockPanel />);
    await flush(5_000);
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("offers no take-control affordance when the backend denies control", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "agent", canControl: false, expiresAt: null, remoteWidth: 1280, remoteHeight: 720 });

    render(<BrowserDockPanel />);
    await flush();

    expect(screen.queryByRole("button", { name: /take control/i })).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders nothing and fires no request until a status body has landed", async () => {
    state.host = false;
    state.runs = RUNNING;

    const { container } = render(<BrowserDockPanel />);
    await flush(30_000);

    expect(get).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });
  it("refreshes the generation after a failed save and preserves the viewer for retry", async () => {
    const human = { active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "one", generation: "g1", mode: "human", isController: true, canControl: true };
    get.mockResolvedValueOnce(human).mockResolvedValue({ ...human, generation: "g2" });
    post.mockRejectedValue(new Error("Could not save"));
    render(<BrowserDockPanel />);
    await flush();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /done & hand back/i })); });
    await flush();
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    expect(screen.getByText(/^Could not save/)).toBeInTheDocument();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /done & hand back/i })); });
    expect(post).toHaveBeenLastCalledWith("/browser/control", { action: "release", generation: "g2" });
  });

  it("keeps a late pre-handoff poll from restoring the old controller", async () => {
    const agent = { active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "one", generation: "g1", mode: "agent", isController: false, canControl: true };
    const human = { ...agent, generation: "g2", mode: "human", isController: true };
    let resolveOld!: (value: typeof agent) => void;
    get.mockResolvedValueOnce(agent).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; })).mockResolvedValue(human);
    post.mockResolvedValue(human);
    render(<BrowserDockPanel />);
    await flush(5_000);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /take control/i })); });
    await flush();
    await act(async () => { resolveOld(agent); });
    expect(screen.getByRole("button", { name: /done & hand back/i })).toBeInTheDocument();
    expect(screen.getByTestId("dcv-live-view").parentElement).not.toHaveAttribute("inert");
  });

  it("drops the previous workspace view even when the API client identity is unchanged", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/one", sessionId: "one", generation: "g1", mode: "human", isController: true }).mockResolvedValue({ active: false });
    const panel = render(<BrowserDockPanel />);
    await flush();
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    state.scope = "org-two";
    panel.rerender(<BrowserDockPanel />);
    await flush();
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    expect(get).toHaveBeenCalledTimes(2);
  });

});
