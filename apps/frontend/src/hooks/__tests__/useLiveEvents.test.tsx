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
const get = vi.fn(async () => ({
  schemaVersion: 1,
  httpEndpoint: "https://events.appsync-api.us-east-1.amazonaws.com/event",
  realtimeEndpoint: "wss://events.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime",
  region: "us-east-1",
  userChannelKey: "11111111-1111-4111-8111-111111111111",
  workspaceId: WORKSPACE_ID,
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock("@/lib/desktopPermissions", () => ({
  readDesktopPermissionGrant: () => ({ permissions: ["screen"] }),
}));
vi.mock("@/lib/desktopPermissionsSubscribe", () => ({ subscribeDesktopPermissions: () => () => {} }));

const shared = vi.hoisted(() => ({
  liveConnected: false,
  connectionRevision: 1,
  roomChannel: null as string | null,
  framesChannel: null as string | null,
  onEvent: null as ((event: { type: string; payload: { resource: string } }) => void) | null,
}));
const statusMutate = vi.hoisted(() => vi.fn());
vi.mock("swr", async (importOriginal) => ({ ...(await importOriginal<typeof import("swr")>()), mutate: statusMutate }));
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return {
    ...actual,
    useLiveEvents: ({ roomChannel, framesChannel, onEvent }: {
      roomChannel: string | null; framesChannel?: string | null; onEvent: typeof shared.onEvent;
    }) => {
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

import { LiveEventsProvider, useLiveEvents } from "../useLiveEvents";

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
      workspaceName: "Acme",
    })));
    expect(shared.liveConnected).toBe(false);
    expect(listen).toHaveBeenCalledWith("node:status", expect.any(Function));
    delete (window as Window & { __TAURI__?: unknown }).__TAURI__;
  });
});
