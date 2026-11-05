import { act, fireEvent, render as renderUI, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = {
  userId: "user-a" as string | null,
  orgId: "org-a" as string | null,
  getToken: vi.fn(async () => "session-token" as string | null),
};
const user = { id: "user-a", fullName: "Alice Example", firstName: "Alice", organizationMemberships: [{ organization: { id: "org-a", name: "Acme" } }] };
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: auth.userId, orgId: auth.orgId, isLoaded: true, isSignedIn: !!auth.userId, getToken: auth.getToken }),
  useUser: () => ({ user }),
}));

const WORKSPACE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const LIVE_CONFIG = {
  schemaVersion: 1,
  httpEndpoint: "https://events.appsync-api.us-east-1.amazonaws.com/event",
  realtimeEndpoint: "wss://events.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime",
  region: "us-east-1",
  userChannelKey: "11111111-1111-4111-8111-111111111111",
  workspaceId: WORKSPACE_ID,
};
const get = vi.fn<(path?: string) => Promise<Record<string, unknown>>>(async () => ({ ...LIVE_CONFIG }));
const post = vi.fn(async () => ({}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get, post, put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock("@/lib/desktopPermissions", () => ({
  readDesktopPermissionGrant: () => ({ permissions: ["screen"] }),
}));
vi.mock("@/lib/desktopPermissionsSubscribe", () => ({ subscribeDesktopPermissions: () => () => {} }));

const shared = vi.hoisted(() => ({
  liveConnected: false,
  connectionRevision: 1,
  roomChannel: null as string | null,
  tasksChannel: false as boolean | undefined,
  framesChannel: null as string | null,
  onEvent: null as ((event: { type: string; payload: { resource: string } }) => void) | null,
  config: null as unknown,
  onConfigStale: null as (() => void) | null,
}));
const statusMutate = vi.hoisted(() => vi.fn());
vi.mock("swr", async (importOriginal) => ({ ...(await importOriginal<typeof import("swr")>()), mutate: statusMutate }));
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return {
    ...actual,
    useLiveEvents: ({ roomChannel, framesChannel, onEvent, config, onConfigStale, tasksChannel }: {
      roomChannel: string | null; framesChannel?: string | null; onEvent: typeof shared.onEvent;
      config: unknown; onConfigStale?: () => void; tasksChannel?: boolean;
    }) => {
      shared.tasksChannel = tasksChannel;
      shared.config = config;
      shared.onConfigStale = onConfigStale ?? null;
      shared.roomChannel = roomChannel;
      shared.framesChannel = framesChannel ?? null;
      shared.onEvent = onEvent;
      return {
        liveConnected: shared.liveConnected,
        connectionRevision: shared.connectionRevision,
        liveChannels: [],
        lastError: null,
      };
    },
  };
});

import { LiveEventsProvider, useLiveEvents, useSuperAgent } from "../useLiveEvents";

function render(ui: ReactElement) {
  return renderUI(<SWRConfig value={{ provider: () => new Map() }}>{ui}</SWRConfig>);
}

function Probe() {
  const live = useLiveEvents();
  return <div>{live.httpReady ? "http-ready" : "http-off"}:{live.liveConnected ? "live" : "reconnecting"}:{live.nodeStatus}</div>;
}

let releaseFirstRoom: (() => void) | null = null;
let releaseSecondRoom: (() => void) | null = null;
let releaseFrames: Array<() => void> = [];

function RoomClaimProbe() {
  const live = useLiveEvents();
  return <>
    <button onClick={() => {
      releaseFirstRoom = live.claimActiveRoomSubscription({ roomId: "room-a", deliveryKey: "key-a" });
    }}>claim first</button>
    <button onClick={() => {
      releaseSecondRoom = live.claimActiveRoomSubscription({ roomId: "room-b", deliveryKey: "key-b" });
    }}>claim second</button>
    <button onClick={() => {
      releaseFirstRoom = live.claimActiveRoomSubscription({ roomId: "room-c", deliveryKey: "key-c", workspaceId: "cccccccc-cccc-4ccc-8ccc-cccccccccccc" });
    }}>claim shared</button>
    <button onClick={() => { releaseFrames.push(live.claimFrames()); }}>claim frames</button>
    <button onClick={() => releaseFrames.shift()?.()}>release frames</button>
    <button onClick={() => releaseFirstRoom?.()}>release first</button>
    <button onClick={() => releaseSecondRoom?.()}>release second</button>
  </>;
}

beforeEach(() => {
  get.mockClear();
  statusMutate.mockClear();
  auth.getToken.mockClear();
  auth.userId = "user-a";
  auth.orgId = "org-a";
  shared.liveConnected = false;
  shared.connectionRevision = 1;
  shared.roomChannel = null;
  releaseFirstRoom = null;
  releaseSecondRoom = null;
  releaseFrames = [];
  shared.framesChannel = null;
  localStorage.setItem("lightbulb.desktop.permissions.v1", JSON.stringify({ permissions: ["screen"] }));
});

describe("LiveEventsProvider", () => {
  it("tells the backend the person is here only while the tab is visible (away-email presence)", async () => {
    post.mockClear();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<LiveEventsProvider><Probe /></LiveEventsProvider>);
    await act(async () => {});
    expect(post).not.toHaveBeenCalledWith("/users/me/active", {});
    visibility.mockReturnValue("visible");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await waitFor(() => expect(post).toHaveBeenCalledWith("/users/me/active", {}));
    visibility.mockRestore();
  });

  it("invalidates only the current account/workspace status key for health updates", async () => {
    await act(async () => { render(<LiveEventsProvider><Probe /></LiveEventsProvider>); });
    shared.onEvent?.({ type: "runtime.resource.invalidated", payload: { resource: "health" } });
    expect(statusMutate).toHaveBeenCalledWith(["/container/status", "user-a:org-a"]);
    statusMutate.mockClear();
    shared.onEvent?.({ type: "runtime.resource.invalidated", payload: { resource: "agents" } });
    expect(statusMutate).not.toHaveBeenCalled();
  });
  it("loads live config for the signed-in user/owner while keeping HTTP ready during AppSync reconnect", async () => {
    render(<LiveEventsProvider><Probe /></LiveEventsProvider>);
    expect(screen.getByText(/http-ready:reconnecting/)).toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledWith("/messaging/live-config"));
  });

  it("drops the live config, and with it the socket, when live-config answers 403 (no longer a member)", async () => {
    render(<LiveEventsProvider><Probe /></LiveEventsProvider>);
    await waitFor(() => expect(shared.config).toMatchObject({ workspaceId: WORKSPACE_ID }));
    const { ApiError } = await import("@lightbulb/shared");
    get.mockRejectedValueOnce(new ApiError(403, { code: "WORKSPACE_MEMBERSHIP_REQUIRED" }));
    await act(async () => { shared.onConfigStale?.(); });
    await waitFor(() => expect(shared.config).toBeNull());
  });

  it("ignores a stale room release after a newer panel claims live delivery", async () => {
    render(<LiveEventsProvider><RoomClaimProbe /></LiveEventsProvider>);

    fireEvent.click(screen.getByRole("button", { name: "claim first" }));
    await waitFor(() => expect(shared.roomChannel).toBe(`/ws/${WORKSPACE_ID}/rooms/room-a/key-a`));
    fireEvent.click(screen.getByRole("button", { name: "claim second" }));
    await waitFor(() => expect(shared.roomChannel).toBe(`/ws/${WORKSPACE_ID}/rooms/room-b/key-b`));

    fireEvent.click(screen.getByRole("button", { name: "release first" }));
    expect(shared.roomChannel).toBe(`/ws/${WORKSPACE_ID}/rooms/room-b/key-b`);
    fireEvent.click(screen.getByRole("button", { name: "release second" }));
    await waitFor(() => expect(shared.roomChannel).toBeNull());
  });

  it("subscribes the active room's frames sibling only while a frames claim is held (ref-counted)", async () => {
    render(<LiveEventsProvider><RoomClaimProbe /></LiveEventsProvider>);
    fireEvent.click(screen.getByRole("button", { name: "claim frames" }));
    fireEvent.click(screen.getByRole("button", { name: "claim frames" }));
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(shared.framesChannel).toBeNull(); // no room yet: nothing to be a sibling of
    fireEvent.click(screen.getByRole("button", { name: "claim first" }));
    await waitFor(() => expect(shared.framesChannel).toBe(`/ws/${WORKSPACE_ID}/frames/room-a/key-a`));
    fireEvent.click(screen.getByRole("button", { name: "claim second" }));
    await waitFor(() => expect(shared.framesChannel).toBe(`/ws/${WORKSPACE_ID}/frames/room-b/key-b`));
    fireEvent.click(screen.getByRole("button", { name: "release frames" }));
    expect(shared.framesChannel).toBe(`/ws/${WORKSPACE_ID}/frames/room-b/key-b`); // one claim still held
    fireEvent.click(screen.getByRole("button", { name: "release frames" }));
    await waitFor(() => expect(shared.framesChannel).toBeNull());
  });

  it("subscribes a shared room on ITS workspace segment, not the caller's", async () => {
    render(<LiveEventsProvider><RoomClaimProbe /></LiveEventsProvider>);

    fireEvent.click(screen.getByRole("button", { name: "claim shared" }));
    await waitFor(() => expect(shared.roomChannel).toBe("/ws/cccccccc-cccc-4ccc-8ccc-cccccccccccc/rooms/room-c/key-c"));
  });

  it.each([
    ["absent", undefined, "off"],
    ["null", null, "off"],
    ["false", { superAgent: false }, "off"],
    ["true", { superAgent: true }, "on"],
  ])("reads the super-agent switch from the live config it already fetches (features %s)", async (_name, features, shown) => {
    get.mockImplementationOnce(async () => ({ ...LIVE_CONFIG, ...(features === undefined ? {} : { features }) }));
    function SwitchProbe() { return <div>switch:{useSuperAgent() ? "on" : "off"}</div>; }
    render(<LiveEventsProvider><SwitchProbe /></LiveEventsProvider>);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(`switch:${shown}`)).toBeTruthy());
    // Off: only the live-config fetch the provider already makes. On: plus one agents-list read (specialist names
    // and the front agent), never a /tasks request.
    expect(get.mock.calls.map(([path]) => path)).toEqual(shown === "on" ? ["/messaging/live-config", "/teams/agents"] : ["/messaging/live-config"]);
  });

  it.each([
    [undefined, false],
    [{ superAgent: true }, true],
  ])("asks for the tasks channel only when the switch is on (features %j)", async (features, on) => {
    get.mockImplementationOnce(async () => ({ ...LIVE_CONFIG, ...(features ? { features } : {}) }));
    render(<LiveEventsProvider><Probe /></LiveEventsProvider>);
    await waitFor(() => expect(shared.config).toBeTruthy());
    expect(shared.tasksChannel).toBe(on);
    // task.changed with nothing cached fetches nothing (no /tasks request while nothing shows tasks).
    act(() => shared.onEvent?.({ type: "task.changed", payload: { taskId: "t1", part: "task", version: 2 } } as never));
    await act(async () => {});
    expect(get.mock.calls.map(([path]) => path)).toEqual(on ? ["/messaging/live-config", "/teams/agents"] : ["/messaging/live-config"]);
  });

  it("useSuperAgent is off outside a provider (a component rendered on its own)", () => {
    function SwitchProbe() { return <div>switch:{useSuperAgent() ? "on" : "off"}</div>; }
    render(<SwitchProbe />);
    expect(screen.getByText("switch:off")).toBeTruthy();
  });

  it("pushes Tauri auth while AppSync is disconnected and again on native node status", async () => {
    const invoke = vi.fn(async () => undefined);
    const listen = vi.fn(async (_name: string, callback: (event: { payload: string }) => void) => {
      callback({ payload: "reconnecting" });
      return () => {};
    });
    Object.assign(window, { __TAURI__: { core: { invoke }, event: { listen } } });
    render(<LiveEventsProvider><Probe /></LiveEventsProvider>);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("send_auth_token", expect.objectContaining({
      token: "session-token",
      userId: "user-a",
      displayName: "Alice Example",
      workspaceId: "org-a",
      workspaceName: "Acme",
    })));
    expect(shared.liveConnected).toBe(false);
    expect(listen).toHaveBeenCalledWith("node:status", expect.any(Function));
    delete (window as Window & { __TAURI__?: unknown }).__TAURI__;
  });
});
