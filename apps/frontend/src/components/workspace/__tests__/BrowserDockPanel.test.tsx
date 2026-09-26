// @vitest-environment jsdom
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

const get = vi.fn();
const post = vi.fn();
const state = vi.hoisted(() => ({
  scope: "org-one",
  host: true,
  participants: [] as { agentActivity: string; id?: string; displayName?: string }[],
  runs: [] as { state: string; runId?: string; agentId?: string }[],
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
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "agent-friday", name: "Friday" }] }),
  agentDisplayName: (agent: { name?: string }) => agent.name ?? "Agent",
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
import { BrowserDockPanel, INPUT_THROTTLE_MS, THREAD_SHARE_KEY } from "../panels/BrowserDockPanel";
import { createPortal } from "react-dom";

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
    post.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 1, mode: "idle" });
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 1, mode: "idle" });
    await act(async () => { screen.getByRole("button", { name: /start browser/i }).click(); });
    expect(post).toHaveBeenCalledWith("/browser/start", {});
    expect(screen.getByRole("button", { name: /^stop$/i })).toBeInTheDocument();
  });

  it("merges fresh metadata while retaining the current signed URL", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://signed/one", sessionId: "s1", generation: 1, mode: "agent" })
      .mockResolvedValue({ active: true, url: "https://signed/one?new", sessionId: "s1", generation: 1, mode: "idle" });
    render(<BrowserDockPanel />);
    await flush();
    await flush(10_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", "https://signed/one");
    expect(screen.getByTestId("browser-bar")).toHaveTextContent(/· idle/);
  });

  it("offers restart recovery for an errored browser", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 4, mode: "error", error: "session unavailable" });
    render(<BrowserDockPanel />);
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("session unavailable");
    post.mockResolvedValue({ active: true, url: "https://signed/new", sessionId: "s2", generation: 1, mode: "idle" });
    await act(async () => { screen.getByRole("button", { name: /restart browser/i }).click(); });
    expect(post).toHaveBeenCalledWith("/browser/start", {});
  });

  it("is one bar: who is browsing, when it saved, Stop and the menu -- no control buttons", async () => {
    const base = { active: true, url: "https://signed", sessionId: "s1", generation: "g1", savedAt: "2026-09-25T21:50:00" };
    get.mockResolvedValue({ ...base, mode: "agent", controllerId: "run-1", agentId: "agent-friday" });
    render(<BrowserDockPanel />);
    await flush();
    const bar = screen.getByTestId("browser-bar");
    expect(bar).toHaveTextContent(/^Shared browser · Friday is browsing · Saved 9:50\sPM/);
    expect(screen.getByRole("button", { name: /^stop$/i })).toHaveAttribute("title", expect.stringMatching(/saves sign-ins/i));
    for (const gone of [/take control/i, /done/i, /^go$/i]) expect(screen.queryByRole("button", { name: gone })).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();

    get.mockResolvedValue({ ...base, mode: "idle" });
    await flush(5_000);
    expect(bar).toHaveTextContent(/^Shared browser · idle · Saved 9:50\sPM/);
  });

  it("passes a person's input straight through while the agent browses, signalling it at most every 2 s", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "agent", agentId: "agent-friday" });
    post.mockResolvedValue({ ok: true });
    render(<BrowserDockPanel />);
    await flush();
    const viewer = screen.getByTestId("browser-viewer");
    // Nothing covers or disables the stream: DCV receives the input itself.
    expect(viewer).not.toHaveAttribute("inert");
    expect(screen.queryByRole("region", { name: /take control|read-only/i })).toBeNull();
    fireEvent.pointerDown(viewer);
    fireEvent.keyDown(viewer, { key: "a" });
    fireEvent.wheel(viewer);
    const inputs = () => post.mock.calls.filter(([path]) => path === "/browser/input");
    expect(inputs()).toEqual([["/browser/input", {}]]);
    await flush(INPUT_THROTTLE_MS);
    fireEvent.keyDown(viewer, { key: "b" });
    expect(inputs()).toHaveLength(2);
    expect(post.mock.calls.every(([path]) => path === "/browser/input")).toBe(true); // no take/renew
  });

  it("stops for any member and keeps the viewer on a failed save", async () => {
    const live = { active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "agent", agentId: "agent-friday" };
    get.mockResolvedValue(live);
    post.mockRejectedValueOnce(new Error("Could not save the browser. Try again."));
    render(<BrowserDockPanel />);
    await flush();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^stop$/i })); });
    expect(post).toHaveBeenCalledWith("/browser/stop", {});
    expect(screen.getByRole("alert")).toHaveTextContent(/could not save/i);
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    post.mockResolvedValue({ active: false, mode: "idle" });
    get.mockResolvedValue({ active: false, mode: "idle" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^stop$/i })); });
    await flush();
    expect(screen.getByText(/no shared browser session is running/i)).toBeInTheDocument();
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

  it("has no address bar of its own: the remote Chrome's bar is the only one", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle" });
    render(<BrowserDockPanel />);
    await flush();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Go" })).toBeNull();
  });

  it("keeps a session connected between polls and reauthenticates for a new session", async () => {
    state.runs = RUNNING;
    const url = "https://browser.test/sessions/one/live-view?signature=first";
    get.mockResolvedValue({ active: true, url, sessionId: "s1", generation: "g1", mode: "agent" });
    render(<BrowserDockPanel />);
    await flush();
    get.mockResolvedValue({ active: true, url: url.replace("first", "second"), sessionId: "s1", generation: "g1", mode: "idle" });
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", url);
    await flush(235_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-initial-url", url.replace("first", "second"));
    const nextUrl = url.replace("/one/", "/two/");
    get.mockResolvedValue({ active: true, url: nextUrl, sessionId: "s2", generation: "g2", mode: "agent" });
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-initial-url", nextUrl);
  });

  it("re-signs and reconnects the viewer when control changes, so watching never goes blank", async () => {
    const url = "https://browser.test/sessions/one/live-view?signature=human";
    get.mockResolvedValue({ active: true, url, sessionId: "s1", generation: "g1", mode: "idle" });
    render(<BrowserDockPanel />);
    await flush();
    const first = screen.getByTestId("dcv-live-view");
    // The agent takes the SAME session: a new generation with a freshly signed URL.
    get.mockResolvedValue({ active: true, url: url.replace("human", "agent"), sessionId: "s1", generation: "g2", mode: "agent" });
    await flush(5_000);
    const second = screen.getByTestId("dcv-live-view");
    expect(second).not.toBe(first); // remounted: a fresh DCV connection
    expect(second).toHaveAttribute("data-initial-url", url.replace("human", "agent"));
    expect(screen.queryByText(/taking control/i)).toBeNull(); // nobody clicked
  });

  it("polls for an autonomous workspace run outside the selected room", async () => {
    state.participants = [{ agentActivity: "running" }];
    get.mockResolvedValue({ active: false });
    render(<BrowserDockPanel />);
    await flush(10_000);
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("keeps discovering the end of a session found outside known runs", async () => {
    get.mockResolvedValue({ active: true, url: "https://browser.test/sessions/other/live-view", sessionId: "s2", generation: "g2", mode: "agent" });
    render(<BrowserDockPanel />);
    await flush();
    get.mockResolvedValue({ active: false });
    await flush(5_000);
    expect(screen.getByText(/no shared browser session is running/i)).toBeInTheDocument();
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("retries a transient failure for a known session without losing the viewer", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "s1", generation: "g1", mode: "agent" })
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ active: true, url: "https://browser.test/sessions/two/live-view", sessionId: "s2", generation: "g2", mode: "agent" });
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
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "s1", generation: "g1", mode: "agent" })
      .mockRejectedValue(new ApiError(403, {}));
    render(<BrowserDockPanel />);
    await flush(5_000);
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("renders nothing and fires no request until a status body has landed", async () => {
    state.host = false;
    state.runs = RUNNING;

    const { container } = render(<BrowserDockPanel />);
    await flush(30_000);

    expect(get).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });
  it("drops the previous workspace view even when the API client identity is unchanged", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/one", sessionId: "one", generation: "g1", mode: "idle" }).mockResolvedValue({ active: false });
    const panel = render(<BrowserDockPanel />);
    await flush();
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    state.scope = "org-two";
    panel.rerender(<BrowserDockPanel />);
    await flush();
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    expect(get).toHaveBeenCalledTimes(2);
  });


  it("docks a thread under the browser, sized by the remembered divider, and gives the height back when it closes", async () => {
    window.localStorage.setItem(THREAD_SHARE_KEY, "0.3");
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle" });
    const Harness = ({ thread }: { thread: boolean }) => {
      const slot = document.querySelector<HTMLElement>("[data-testid=browser-thread-slot]");
      return <>{thread && slot && createPortal(<aside aria-label="Thread">replies</aside>, slot)}<BrowserDockPanel /></>;
    };
    const { rerender } = render(<Harness thread={false} />);
    await flush();
    const slot = screen.getByTestId("browser-thread-slot");
    const divider = screen.getByRole("separator", { name: /resize the browser and the thread/i });
    expect(slot.style.flexBasis).toBe("30%");
    // Empty: the slot and the divider hide, so the browser fills the panel.
    expect(slot).toHaveClass("empty:hidden");
    expect(divider).toHaveClass("peer-empty:hidden");
    expect(slot.matches(":empty")).toBe(true);
    rerender(<Harness thread />);
    expect(slot).toHaveTextContent("replies");
    expect(slot.matches(":empty")).toBe(false);
    fireEvent.keyDown(divider, { key: "ArrowUp" });
    expect(slot.style.flexBasis).toBe("35%");
    expect(window.localStorage.getItem(THREAD_SHARE_KEY)).toBe("0.35");
    rerender(<Harness thread={false} />); // thread closed
    expect(slot.matches(":empty")).toBe(true);
  });
});
