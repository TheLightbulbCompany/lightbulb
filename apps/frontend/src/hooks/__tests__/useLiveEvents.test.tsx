import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = {
  userId: "user-a" as string | null,
  orgId: "org-a" as string | null,
  getToken: vi.fn(async () => "session-token" as string | null),
};
const user = { id: "user-a", fullName: "Alice Example", firstName: "Alice" };
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: auth.userId, orgId: auth.orgId, isSignedIn: !!auth.userId, getToken: auth.getToken }),
  useUser: () => ({ user }),
}));

const get = vi.fn(async () => ({
  schemaVersion: 1,
  httpEndpoint: "https://events.appsync-api.us-east-1.amazonaws.com/event",
  realtimeEndpoint: "wss://events.appsync-realtime-api.us-east-1.amazonaws.com/event/realtime",
  region: "us-east-1",
  userChannelKey: "11111111-1111-4111-8111-111111111111",
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get, post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock("@/lib/desktopPermissions", () => ({
  readDesktopPermissionGrant: () => ({ permissions: ["screen"] }),
}));
vi.mock("@/lib/desktopPermissionsSubscribe", () => ({ subscribeDesktopPermissions: () => () => {} }));

const shared = vi.hoisted(() => ({ liveConnected: false, connectionRevision: 1 }));
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return {
    ...actual,
    useLiveEvents: () => ({
      liveConnected: shared.liveConnected,
      lastEventAt: null,
      connectionRevision: shared.connectionRevision,
      liveChannels: [],
      lastError: null,
    }),
  };
});

import { LiveEventsProvider, useLiveEvents } from "../useLiveEvents";

function Probe() {
  const live = useLiveEvents();
  return <div>{live.httpReady ? "http-ready" : "http-off"}:{live.liveConnected ? "live" : "reconnecting"}:{live.nodeStatus}</div>;
}

beforeEach(() => {
  get.mockClear();
  auth.getToken.mockClear();
  auth.userId = "user-a";
  auth.orgId = "org-a";
  shared.liveConnected = false;
  shared.connectionRevision = 1;
  localStorage.setItem("lightbulb.desktop.permissions.v1", JSON.stringify({ permissions: ["screen"] }));
});

describe("LiveEventsProvider", () => {
  it("loads live config for the signed-in user/owner while keeping HTTP ready during AppSync reconnect", async () => {
    render(<LiveEventsProvider><Probe /></LiveEventsProvider>);
    expect(screen.getByText(/http-ready:reconnecting/)).toBeInTheDocument();
    await waitFor(() => expect(get).toHaveBeenCalledWith("/messaging/live-config"));
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
    })));
    expect(shared.liveConnected).toBe(false);
    expect(listen).toHaveBeenCalledWith("node:status", expect.any(Function));
    delete (window as Window & { __TAURI__?: unknown }).__TAURI__;
  });
});
