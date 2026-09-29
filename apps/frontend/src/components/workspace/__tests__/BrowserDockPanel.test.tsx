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
  runs: [] as { state: string; runId?: string; agentId?: string; tools: { name: string; args?: unknown; result?: unknown; status?: string }[] }[],
}));

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ orgId: state.scope, userId: "human" }) }));
vi.mock("@lightbulb/shared", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@lightbulb/shared")>()),
  useStatusLoaded: () => state.host,
}));
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
// The bar's popovers mount the Connections sections themselves; stand-ins record their props.
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));
vi.mock("@/components/control/panels/BrowserExtensionsSection", () => ({
  BrowserExtensionsSection: ({ onChanged }: { onChanged?: () => void }) =>
    <button type="button" onClick={onChanged}>extensions-section</button>,
}));
vi.mock("@/components/control/panels/WebsiteLoginsSection", () => ({
  WebsiteLoginsSection: ({ canManage }: { canManage: boolean }) => <p>logins-section {String(canManage)}</p>,
}));
vi.mock("@/components/messaging/NewSessionButton", () => ({
  useTargets: () => ({ data: { enabled: true, devices: [{ deviceId: "mac-1", generation: "g", name: "Studio Mac" }] } }),
}));
vi.mock("../panels/BrowserFrameView", () => ({
  BrowserFrameView: ({ runId, heartbeat, mac, prefer }: { runId?: string; heartbeat?: string; mac?: { deviceId: string }; prefer?: string }) =>
    <div data-testid="mac-frames" data-run={runId} data-heartbeat={heartbeat} data-mac={mac?.deviceId} data-prefer={prefer} />,
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
import { BrowserDockPanel, INPUT_THROTTLE_MS, SAVE_TIMEOUT_MS, THREAD_SHARE_KEY, VIEW_HEARTBEAT_MS } from "../panels/BrowserDockPanel";
import { createPortal } from "react-dom";
import { SharedLiveViewHost, _resetSharedLiveViewForTests } from "../panels/sharedLiveView";
import { _resetControllerForTests, selectBrowserMacRun } from "../workspaceController";

// The panel shows the page's one shared live-view client, which the chat page mounts.
function Panel(props: React.ComponentProps<typeof BrowserDockPanel>): React.ReactElement {
  return <><SharedLiveViewHost /><BrowserDockPanel {...props} /></>;
}

// RTL's waitFor/findBy* only detect JEST fake timers (`typeof jest !== 'undefined'`
// in @testing-library/dom 10.4), so under vitest's they hang on a faked
// setInterval. Every wait here therefore drives the clock explicitly.
async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

const RUNNING = [{ state: "running", tools: [] }];

function setPageVisibility(value: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { value, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

const viewingCalls = () => post.mock.calls.filter(([path]) => path === "/browser/viewing").length;

beforeEach(() => {
  vi.useFakeTimers();
  _resetSharedLiveViewForTests();
  // Hidden by default so the viewer heartbeat stays out of tests about other requests.
  Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
  get.mockReset();
  post.mockReset();
  state.host = true;
  state.scope = "org-one";
  state.runs = [];
  state.participants = [];
  _resetControllerForTests();
});

describe("BrowserDockPanel", () => {
  it("polls every 5s while a run is active, and stops when it ends", async () => {
    // Browser sessions live 4-15s (design §2g / ruling R14). A 240s poll would
    // catch one ~6% of the time, so the panel would read empty while the agent
    // was visibly browsing. 240s is the RE-MINT bound for a URL already on
    // screen, not a poll interval.
    get.mockResolvedValue({ active: false, url: null, expiresAt: null, remoteWidth: 1920, remoteHeight: 1080 });
    state.runs = RUNNING;

    const view = render(<Panel />);
    await flush(15_000);
    expect(get).toHaveBeenCalledTimes(4); // mount + 3
    expect(get).toHaveBeenCalledWith("/browser/live-view", expect.objectContaining({ signal: expect.any(AbortSignal) }));

    state.runs = [];
    view.rerender(<Panel />);
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

    render(<Panel />);
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

    render(<Panel />);
    await flush();

    expect(screen.getByText(/no shared browser session is running/i)).toBeInTheDocument();
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
  });

  it("explicitly starts an inactive shared browser and never starts on mount", async () => {
    get.mockResolvedValue({ active: false, mode: "idle" });
    render(<Panel />);
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
    render(<Panel />);
    await flush();
    await flush(10_000);
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", "https://signed/one");
    expect(screen.getByTestId("browser-bar")).toHaveTextContent(/· idle/);
  });

  it("offers restart recovery for an errored browser", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: 4, mode: "error", error: "session unavailable" });
    render(<Panel />);
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent("session unavailable");
    post.mockResolvedValue({ active: true, url: "https://signed/new", sessionId: "s2", generation: 1, mode: "idle" });
    await act(async () => { screen.getByRole("button", { name: /restart browser/i }).click(); });
    expect(post).toHaveBeenCalledWith("/browser/start", {});
  });

  it("is one bar: who is browsing, when it saved, Stop and the menu -- no control buttons", async () => {
    const base = { active: true, url: "https://signed", sessionId: "s1", generation: "g1", savedAt: "2026-09-25T21:50:00" };
    get.mockResolvedValue({ ...base, mode: "agent", agentId: "agent-friday" });
    render(<Panel />);
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

  it("opens Extensions and Website logins from the bar, and polls at once after an extension change", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle" });
    render(<Panel />);
    await flush();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Website logins" })); });
    expect(screen.getByText("logins-section true")).toBeInTheDocument();
    await act(async () => { fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" }); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Extensions" })); });
    const polls = get.mock.calls.length;
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "extensions-section" })); });
    await flush();
    // The restart shows without waiting for the next 5 s poll.
    expect(get.mock.calls.length).toBeGreaterThan(polls);
  });

  it("passes a person's input straight through while the agent browses, signalling it at most every 2 s", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "agent", agentId: "agent-friday" });
    post.mockResolvedValue({ ok: true });
    render(<Panel />);
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
    render(<Panel />);
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

  it("has no address bar of its own: the remote Chrome's bar is the only one", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle" });
    render(<Panel />);
    await flush();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Go" })).toBeNull();
  });

  it("keeps a session connected between polls and reauthenticates for a new session", async () => {
    state.runs = RUNNING;
    const url = "https://browser.test/sessions/one/live-view?signature=first";
    get.mockResolvedValue({ active: true, url, sessionId: "s1", generation: "g1", mode: "agent" });
    render(<Panel />);
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

  it("keeps one viewer per session: a control change hands it a fresh URL but never reconnects it", async () => {
    const url = "https://browser.test/sessions/one/live-view?signature=human";
    get.mockResolvedValue({ active: true, url, sessionId: "s1", generation: "g1", mode: "idle" });
    render(<Panel />);
    await flush();
    const first = screen.getByTestId("dcv-live-view");
    // The agent takes the SAME session: a new generation with a freshly signed URL.
    get.mockResolvedValue({ active: true, url: url.replace("human", "agent"), sessionId: "s1", generation: "g2", mode: "agent" });
    await flush(5_000);
    // An autosave: yet another generation.
    get.mockResolvedValue({ active: true, url: url.replace("human", "saved"), sessionId: "s1", generation: "g3", mode: "idle" });
    await flush(5_000);
    const viewer = screen.getByTestId("dcv-live-view");
    expect(viewer).toBe(first); // same mount: the DCV connection is not torn down (no flicker)
    expect(viewer).toHaveAttribute("data-signed-url", url.replace("human", "saved")); // kept for a reconnect
    expect(screen.queryByText(/taking control/i)).toBeNull(); // nobody clicked
  });

  it("polls for an autonomous workspace run outside the selected room", async () => {
    state.participants = [{ agentActivity: "running" }];
    get.mockResolvedValue({ active: false });
    render(<Panel />);
    await flush(10_000);
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("says why the browser stopped and what was saved, and any click starts it again", async () => {
    const live = { active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle", savedAt: "2026-09-26T16:41:00" };
    get.mockResolvedValue(live);
    render(<Panel />);
    await flush();
    // Our idle stop: the next poll replaces the (now frozen) picture with the reason.
    get.mockResolvedValue({ active: false, url: null, sessionId: null, generation: "g2", mode: "idle", savedAt: live.savedAt, endedReason: "idle", idleStopSeconds: 1800 });
    await flush(5_000);
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    expect(screen.getByTestId("browser-stopped")).toHaveTextContent(/Browser stopped after 30 min idle · state saved 4:41\sPM/);
    post.mockResolvedValue({ ...live, generation: "g3" });
    await act(async () => { fireEvent.click(screen.getByTestId("browser-stopped")); });
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith("/browser/start", {});
  });

  it("says Restarting while a new extension restarts the browser, and keeps watching until it is back", async () => {
    const savedAt = new Date().toISOString();
    get.mockResolvedValue({ active: false, url: null, sessionId: null, generation: "g2", mode: "idle", savedAt, endedReason: "restart" });
    render(<Panel />);
    await flush();
    expect(screen.getByText("Restarting the browser to load its extensions…")).toBeInTheDocument();
    expect(screen.queryByTestId("browser-stopped")).toBeNull();
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s2", generation: "g3", mode: "idle", savedAt });
    await flush(5_000);
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
  });

  it("falls back to Start when a restart never came back", async () => {
    get.mockResolvedValue({ active: false, url: null, sessionId: null, generation: "g2", mode: "idle", savedAt: "2026-09-26T16:41:00", endedReason: "restart" });
    render(<Panel />);
    await flush();
    expect(screen.getByTestId("browser-stopped")).toBeInTheDocument();
  });

  it("names a session that ended at AWS", async () => {
    get.mockResolvedValue({ active: false, url: null, sessionId: "s1", generation: "g1", mode: "idle", savedAt: "2026-09-26T16:41:00", endedReason: "expired" });
    render(<Panel />);
    await flush();
    expect(screen.getByTestId("browser-stopped")).toHaveTextContent(/Browser session ended · state saved 4:41\sPM/);
  });

  it("turns a save still running after 30 s into Save failed with a retry", async () => {
    const saving = { active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "transition", operation: "save" };
    get.mockResolvedValue(saving);
    render(<Panel />);
    await flush();
    const bar = screen.getByTestId("browser-bar");
    expect(bar).toHaveTextContent(/· Saving…/);
    await flush(SAVE_TIMEOUT_MS);
    expect(bar).toHaveTextContent(/· Save failed/);
    post.mockResolvedValue({ ok: true });
    await act(async () => { screen.getByRole("button", { name: /retry save/i }).click(); });
    expect(post).toHaveBeenCalledWith("/browser/input", {});
    get.mockResolvedValue({ ...saving, generation: "g2", mode: "idle", operation: null, savedAt: "2026-09-26T16:45:00" });
    await flush(5_000);
    expect(bar).toHaveTextContent(/· idle · Saved 4:45\sPM/);
  });

  it("says Starting, not Saving, while a session starts", async () => {
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "transition", operation: "start" });
    render(<Panel />);
    await flush();
    await flush(SAVE_TIMEOUT_MS);
    expect(screen.getByTestId("browser-bar")).toHaveTextContent(/· Starting…/);
  });

  it("keeps discovering the end of a session found outside known runs", async () => {
    get.mockResolvedValue({ active: true, url: "https://browser.test/sessions/other/live-view", sessionId: "s2", generation: "g2", mode: "agent" });
    render(<Panel />);
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
    render(<Panel />);
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
    const panel = render(<Panel />);
    await flush(15_000);
    expect(get).toHaveBeenCalledTimes(2);
    expect(get.mock.calls[0][1].signal.aborted).toBe(true);
    panel.unmount();
    expect(get.mock.calls[1][1].signal.aborted).toBe(true);
  });

  it("clears the viewer and stops polling when access is revoked", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/sessions/one/live-view", sessionId: "s1", generation: "g1", mode: "agent" })
      .mockRejectedValue(new ApiError(403, {}));
    render(<Panel />);
    await flush(5_000);
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    await flush(30_000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("renders nothing and fires no request until a status body has landed", async () => {
    state.host = false;
    state.runs = RUNNING;

    const { container } = render(<Panel />);
    await flush(30_000);

    expect(get).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });
  it("drops the previous workspace view even when the API client identity is unchanged", async () => {
    get.mockResolvedValueOnce({ active: true, url: "https://browser.test/one", sessionId: "one", generation: "g1", mode: "idle" }).mockResolvedValue({ active: false });
    const panel = render(<Panel />);
    await flush();
    expect(screen.getByTestId("dcv-live-view")).toBeInTheDocument();
    state.scope = "org-two";
    panel.rerender(<Panel />);
    await flush();
    expect(screen.queryByTestId("dcv-live-view")).toBeNull();
    expect(get).toHaveBeenCalledTimes(2);
  });


  it("docks a thread under the browser, sized by the remembered divider, and gives the height back when it closes", async () => {
    window.localStorage.setItem(THREAD_SHARE_KEY, "0.3");
    get.mockResolvedValue({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle" });
    const Harness = ({ thread }: { thread: boolean }) => {
      const slot = document.querySelector<HTMLElement>("[data-testid=browser-thread-slot]");
      return <>{thread && slot && createPortal(<aside aria-label="Thread">replies</aside>, slot)}<Panel /></>;
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

  it("sends the viewer heartbeat only while the page and the dock tab are visible", async () => {
    get.mockResolvedValue({ active: false, mode: "idle", generation: "g1" });
    post.mockResolvedValue({ active: false, mode: "transition", operation: "start", generation: "g2" });
    let dockListener: (event: { isVisible: boolean }) => void = () => undefined;
    const panelApi = {
      isVisible: true,
      onDidVisibilityChange: (listener: typeof dockListener) => { dockListener = listener; return { dispose: () => undefined }; },
    };
    setPageVisibility("visible");
    render(<Panel api={panelApi as never} />);
    await flush();
    expect(viewingCalls()).toBe(1); // opening it starts the browser: no click
    await flush(VIEW_HEARTBEAT_MS);
    expect(viewingCalls()).toBe(2);

    setPageVisibility("hidden");
    await flush(VIEW_HEARTBEAT_MS * 3);
    expect(viewingCalls()).toBe(2);
    setPageVisibility("visible");
    await flush();
    expect(viewingCalls()).toBe(3);

    act(() => dockListener({ isVisible: false })); // tabbed away in the dock
    await flush(VIEW_HEARTBEAT_MS * 3);
    expect(viewingCalls()).toBe(3);
  });

  it("applies an active heartbeat immediately, without waiting for a poll tick", async () => {
    // A fast cold start can already be live by the second heartbeat: the panel must not
    // wait for the 5s GET poll (which nothing here ever re-arms) to notice.
    get.mockResolvedValue({ active: false, mode: "idle" });
    post.mockResolvedValueOnce({ active: false, mode: "idle" })
      .mockResolvedValueOnce({ active: true, url: "https://signed", sessionId: "s1", generation: "g1", mode: "idle" });
    setPageVisibility("visible");
    render(<Panel />);
    await flush();
    expect(screen.getByTestId("browser-stopped")).toBeInTheDocument();

    const getCallsBefore = get.mock.calls.length;
    await flush(VIEW_HEARTBEAT_MS); // next heartbeat tick lands active
    expect(get.mock.calls.length).toBe(getCallsBefore); // no poll tick needed
    expect(screen.getByTestId("dcv-live-view")).toHaveAttribute("data-signed-url", "https://signed");
  });

  it("keeps polling while the browser the heartbeat started is starting", async () => {
    get.mockResolvedValue({ active: false, mode: "transition", operation: "start", generation: "g2" });
    post.mockResolvedValue({ active: false, mode: "transition", operation: "start", generation: "g2" });
    setPageVisibility("visible");
    render(<Panel />);
    await flush();
    const before = get.mock.calls.length;
    await flush(10_000);
    expect(get.mock.calls.length).toBeGreaterThanOrEqual(before + 2);
    expect(screen.getByText("Starting…")).toBeTruthy();
  });

  it("shows a Mac run's tab live and interactive, starts no cloud browser, and falls back to the cloud when the run ends", async () => {
    get.mockResolvedValue({ active: false, mode: "idle" });
    post.mockResolvedValue({ active: false, mode: "idle" });
    const macRun = { state: "running", runId: "run-mac", agentId: "agent-friday",
      tools: [{ name: "lightbulb_desktop", args: { action: "use_browser" }, status: "done",
        result: JSON.stringify({ browser: "mac", deviceId: "mac-1" }) }] };
    state.runs = [macRun];
    act(() => selectBrowserMacRun("run-mac"));
    setPageVisibility("visible");
    const view = render(<Panel />);
    await flush();

    expect(screen.getByTestId("mac-browser-bar")).toHaveTextContent(
      "Friday · on Studio Mac · click or type to take over; Friday waits while you do");
    const frames = screen.getByTestId("mac-frames");
    expect(frames).toHaveAttribute("data-run", "run-mac");
    expect(frames).toHaveAttribute("data-mac", "mac-1");
    expect(frames).toHaveAttribute("data-prefer", "view");
    expect(viewingCalls()).toBe(0); // watching a Mac never starts a cloud browser

    // Cloud and Mac are tabs: switching back shows the cloud browser.
    fireEvent.click(screen.getByRole("tab", { name: "Cloud browser" }));
    await flush();
    expect(screen.queryByTestId("mac-frames")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Friday · Studio Mac" }));
    expect(screen.getByTestId("mac-frames")).toBeInTheDocument();

    state.runs = [];
    view.rerender(<Panel />);
    await flush();
    expect(screen.queryByTestId("mac-frames")).toBeNull();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByText(/no shared browser session is running/i)).toBeInTheDocument();
  });

  it("panel tab names the Mac, else falls back to a Mac", async () => {
    get.mockResolvedValue({ active: false, mode: "idle" });
    post.mockResolvedValue({ active: false, mode: "idle" });
    const useBrowserResult = (obj: unknown) => [{ type: "text", text: JSON.stringify(obj) }];
    // Named: the acquire's deviceName wins outright, even over an id that happens to match the devices list.
    const namedRun = { state: "running", runId: "run-named", agentId: "agent-friday",
      tools: [{ name: "lightbulb_desktop", args: { action: "use_browser" }, status: "done",
        result: useBrowserResult({ browser: "mac", deviceId: "mac-1", deviceName: "Prasiddha's Mac" }) }] };
    // No name and no matching device: falls all the way back to "a Mac".
    const unnamedRun = { state: "running", runId: "run-unnamed", agentId: "agent-friday",
      tools: [{ name: "lightbulb_desktop", args: { action: "use_browser" }, status: "done",
        result: useBrowserResult({ browser: "mac", deviceId: "mac-unknown", deviceName: null }) }] };
    state.runs = [namedRun, unnamedRun];
    setPageVisibility("visible");
    render(<Panel />);
    await flush();

    expect(screen.getByRole("tab", { name: "Friday · Prasiddha's Mac" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Friday · a Mac" })).toBeInTheDocument();
  });
});
