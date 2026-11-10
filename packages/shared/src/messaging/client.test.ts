import { describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiMethods } from "../api/client";
import { createMessagingClient } from "./client";
import { LIVE_EVENT_TYPES, parseLiveEvent } from "../live/types";

function setup() {
  const methods = {
    get: vi.fn(async () => ({})),
    post: vi.fn(async () => ({})),
    put: vi.fn(async () => ({})),
    patch: vi.fn(async () => ({})),
    del: vi.fn(async () => null),
  };
  return { api: methods as unknown as ApiMethods, methods, client: createMessagingClient(methods as unknown as ApiMethods) };
}

const roomId = "11111111-1111-4111-8111-111111111111";
const agentId = "22222222-2222-4222-8222-222222222222";
const messageId = "33333333-3333-4333-8333-333333333333";

describe("messaging HTTP client", () => {
  it("opens a session's chat through its own door", async () => {
    const { client, methods } = setup();
    await client.openSessionChat("sess 1");
    expect(methods.post).toHaveBeenCalledWith("/messaging/sessions/sess%201/chat", {});
  });

  it("reads Activity with an opaque cursor, marks it seen and runs one escaped action door", async () => {
    const { client, methods } = setup();
    await client.listActivity();
    await client.listActivity({ cursor: "MjAyNi0wOS0yNA", limit: 50 });
    await client.markActivitySeen("2026-09-24T10:00:00Z");
    await client.runActivityAction("paperclip:approval:ap-1", "approve", { note: "go" });
    await client.runActivityAction("mention:m:r:1", "mark_done");
    expect(methods.get.mock.calls).toEqual([
      ["/messaging/activity"],
      ["/messaging/activity?cursor=MjAyNi0wOS0yNA&limit=50"],
    ]);
    expect(methods.post.mock.calls).toEqual([
      ["/messaging/activity/seen", { upTo: "2026-09-24T10:00:00Z" }],
      ["/messaging/activity/paperclip%3Aapproval%3Aap-1/actions/approve", { note: "go" }],
      ["/messaging/activity/mention%3Am%3Ar%3A1/actions/mark_done", {}],
    ]);
  });

  it("removes one ended session with DELETE", async () => {
    const { client, methods } = setup();
    methods.del.mockResolvedValue({ removed: 0 } as never);
    await client.removeLocalSession("session/b");
    expect(methods.del.mock.calls).toEqual([["/messaging/sessions/session%2Fb"]]);
  });

  it("uses workspace-scoped session routes and preserves command identity with escaped path segments", async () => {
    const { client, methods } = setup();
    const command = { requestId: "request-1", expectedGeneration: "generation-1", kind: "send" as const, text: "continue" };
    await client.listLocalSessionEvents("session/b", 3, 20);
    await client.submitLocalSessionCommand("session/b", command);
    await client.submitLocalSessionCommand("session/b", command, "old/request");
    await client.getLocalSessionCommand("session/b", "request/c");
    await client.listLocalSessionOutbox("session/b");
    await client.cancelLocalSessionCommand("session/b", "request/c");
    expect(methods.get.mock.calls).toEqual([
      ["/messaging/sessions/session%2Fb/events?afterSeq=3&limit=20"],
      ["/messaging/sessions/session%2Fb/commands/request%2Fc"],
      ["/messaging/sessions/session%2Fb/outbox"],
    ]);
    expect(methods.post).toHaveBeenCalledWith("/messaging/sessions/session%2Fb/commands", command);
    expect(methods.post).toHaveBeenCalledWith("/messaging/sessions/session%2Fb/commands?replacesRequestId=old%2Frequest", command);
    expect(methods.post).toHaveBeenCalledWith("/messaging/sessions/session%2Fb/commands/request%2Fc/cancel", {});
  });

  it("addresses resume and its revocation", async () => {
    const { client, methods } = setup();
    await client.resumeLocalSession("s/1", { resumeId: "r-1", message: "go on" });
    await client.revokeLocalSessionResume("s/1", "user/bob");
    expect(methods.post).toHaveBeenCalledWith("/messaging/sessions/s%2F1/resume", { resumeId: "r-1", message: "go on" });
    expect(methods.del).toHaveBeenCalledWith("/messaging/sessions/s%2F1/resume-allowed/user%2Fbob");
  });

  it("uses the frozen read endpoints and sequence query", async () => {
    const { client, methods } = setup();
    await client.getLiveConfig();
    await client.listRooms();
    await client.getRoom(roomId);
    await client.listMessages(roomId, 42, 100);
    await client.getRunStatus("run-1");
    expect(methods.get.mock.calls).toEqual([
      ["/messaging/live-config"],
      ["/messaging/rooms"],
      [`/messaging/rooms/${roomId}`],
      [`/messaging/rooms/${roomId}/messages?afterSeq=42&limit=100`, { signal: expect.any(AbortSignal) }],
      ["/messaging/runs/run-1"],
    ]);
  });

  it("passes stable caller-owned idempotency IDs and canonical member UUIDs unchanged", async () => {
    const { client, methods } = setup();
    const create = {
      clientRoomId: roomId,
      name: "Launch",
      kind: "channel" as const,
      members: [{ kind: "agent" as const, id: agentId, role: "member" as const, listenMode: "active" as const }],
    };
    const send = {
      clientMessageId: messageId,
      text: "@friday check launch",
      mentions: [{ kind: "agent" as const, id: agentId }],
      replyToMessageId: null,
      threadRootMessageId: null,
    };
    await client.createRoom(create);
    await client.sendMessage(roomId, send);
    expect(methods.post).toHaveBeenNthCalledWith(1, "/messaging/rooms", create);
    // sendMessage also passes an AbortSignal (see the timeout test below) --
    // a bare-body createRoom call carries no third argument at all.
    expect(methods.post).toHaveBeenNthCalledWith(
      2, `/messaging/rooms/${roomId}/messages`, send, { signal: expect.any(AbortSignal) },
    );
  });

  it("aborts a hung send after its timeout instead of letting the fetch hang forever", () => {
    // Regression: a prod send whose POST never settled (server had already
    // stored + returned 201) left the optimistic row on "Sending..." forever.
    // This only proves the client-side half -- that a hung fetch eventually
    // rejects; useRooms.test.tsx covers the resulting "uncertain" state.
    vi.useFakeTimers();
    try {
      const capturedSignals: AbortSignal[] = [];
      const methods = {
        get: vi.fn(async () => ({})),
        post: vi.fn((_path: string, _body: unknown, opts?: { signal?: AbortSignal }) => {
          if (opts?.signal) capturedSignals.push(opts.signal);
          return new Promise(() => {}); // never settles, like the hung fetch in prod
        }),
        put: vi.fn(async () => ({})),
        patch: vi.fn(async () => ({})),
        del: vi.fn(async () => null),
      };
      const client = createMessagingClient(methods as unknown as ApiMethods);
      void client.sendMessage(roomId, {
        clientMessageId: messageId, text: "hi", mentions: [],
        replyToMessageId: null, threadRootMessageId: null,
      });
      expect(capturedSignals).toHaveLength(1);
      expect(capturedSignals[0].aborted).toBe(false);
      vi.advanceTimersByTime(30_000);
      expect(capturedSignals[0].aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("calls the subagent routes", async () => {
    const { client, methods } = setup();
    await client.listSubagents("r1");
    await client.subagentTail("r1", "a1", "sa-1");
    await client.steerSubagent("r1", "a1", "sa-1", "focus");
    await client.stopSubagent("r1", "a1", "sa-1");
    expect(methods.get).toHaveBeenCalledWith("/messaging/rooms/r1/subagents");
    expect(methods.get).toHaveBeenCalledWith("/messaging/rooms/r1/agents/a1/subagents/sa-1/tail");
    expect(methods.post).toHaveBeenCalledWith("/messaging/rooms/r1/agents/a1/subagents/sa-1/steer", { text: "focus" });
    expect(methods.post).toHaveBeenCalledWith("/messaging/rooms/r1/agents/a1/subagents/sa-1/stop", {});
  });

  it("uses exact mutation verbs, paths, and bodies without server-owned owner/author fields", async () => {
    const { client, methods } = setup();
    await client.updateRoom(roomId, { name: null });
    await client.updateMessage(roomId, messageId, { operationId: messageId, expectedRevision: 2, text: null });
    await client.upsertMember(roomId, { kind: "agent", id: agentId, role: "member", listenMode: "mentions" });
    await client.removeMember(roomId, "agent", agentId);
    await client.markRead(roomId, { lastReadSeq: 9 });
    await client.markUnread(roomId);
    await client.setTyping({ roomId, isTyping: true });
    await client.abortRun("run-1");
    await client.steerRun("run-1", { text: "focus on checkout" });

    expect(methods.patch).toHaveBeenCalledWith(`/messaging/rooms/${roomId}`, { name: null });
    expect(methods.patch).toHaveBeenCalledWith(`/messaging/rooms/${roomId}/messages/${messageId}`, { operationId: messageId, expectedRevision: 2, text: null });
    expect(methods.post).toHaveBeenCalledWith(`/messaging/rooms/${roomId}/members`, {
      kind: "agent", id: agentId, role: "member", listenMode: "mentions",
    });
    expect(methods.del).toHaveBeenCalledWith(`/messaging/rooms/${roomId}/members/agent/${agentId}`);
    expect(methods.put).toHaveBeenCalledWith(`/messaging/rooms/${roomId}/read`, { lastReadSeq: 9 });
    expect(methods.post).toHaveBeenCalledWith(`/messaging/rooms/${roomId}/unread`, {});
    expect(methods.post).toHaveBeenCalledWith("/messaging/typing", { roomId, isTyping: true });
    expect(methods.post).toHaveBeenCalledWith("/messaging/runs/run-1/abort", {});
    expect(methods.post).toHaveBeenCalledWith("/messaging/runs/run-1/steer", { text: "focus on checkout" });
    expect(JSON.stringify(methods)).not.toContain("ownerId");
  });

  it("archives and unarchives through the room PATCH", async () => {
    const { client, methods } = setup();
    await client.updateRoom(roomId, { archived: true });
    await client.updateRoom(roomId, { archived: false });
    expect(methods.patch.mock.calls).toEqual([
      [`/messaging/rooms/${roomId}`, { archived: true }],
      [`/messaging/rooms/${roomId}`, { archived: false }],
    ]);
  });

  it("addresses the phase 3 room commands", async () => {
    const calls: Array<[string, string, unknown]> = [];
    const api = {
      get: async (path: string) => { calls.push(["get", path, undefined]); return {}; },
      post: async (path: string, body?: unknown) => { calls.push(["post", path, body]); return {}; },
      put: async (path: string, body?: unknown) => { calls.push(["put", path, body]); return {}; },
      patch: async (path: string, body?: unknown) => { calls.push(["patch", path, body]); return {}; },
      del: async (path: string) => { calls.push(["del", path, undefined]); return {}; },
    } as never;
    const client = createMessagingClient(api);
    await client.setMemberPrefs("r 1", { starred: true });
    await client.putSections({ sections: [{ id: "s-1", name: "Ship" }] });
    await client.leaveRoom("r 1");
    await client.deleteRoom("r 1");
    await client.setPin("r 1", "m 1", true);
    await client.setPin("r 1", "m 1", false);
    await client.markUnread("r 1", 4);
    await client.markUnread("r 1");
    expect(calls).toEqual([
      ["patch", "/messaging/rooms/r%201/me", { starred: true }],
      ["put", "/messaging/me/sections", { sections: [{ id: "s-1", name: "Ship" }] }],
      ["post", "/messaging/rooms/r%201/leave", {}],
      ["del", "/messaging/rooms/r%201", undefined],
      ["put", "/messaging/rooms/r%201/pins/m%201", {}],
      ["del", "/messaging/rooms/r%201/pins/m%201", undefined],
      ["post", "/messaging/rooms/r%201/unread", { fromSeq: 4 }],
      ["post", "/messaging/rooms/r%201/unread", {}],
    ]);
  });
});

it("recognises the member-updated live event", () => {
  expect(LIVE_EVENT_TYPES.has("room.member.updated")).toBe(true);
});

it("parses a room.member.updated frame and rejects a malformed one", () => {
  // The backend publishes this on the user channel, not a room subscription,
  // so (unlike other room.* events) the envelope itself carries no roomId --
  // only payload.roomId does. See core/services/messaging.py's patch_room_me.
  const event = {
    schemaVersion: 1, eventId: "member-updated-1", type: "room.member.updated", occurredAt: "now",
    payload: { roomId: "room-1" },
  };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: {} })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { roomId: 42 } })).toBeNull();
  // Relaxing the cross-check waives only an ABSENT envelope roomId: one that
  // is present and disagrees with the payload is still a rejected frame.
  expect(parseLiveEvent({ ...event, roomId: "other" })).toBeNull();
  expect(parseLiveEvent({ ...event, roomId: "room-1" })).toEqual({ ...event, roomId: "room-1" });
});

it("bounds history reads even when authentication never settles", async () => {
  vi.useFakeTimers();
  try {
    const getToken = vi.fn(() => new Promise<string | null>(() => {}));
    const client = createMessagingClient(createApiClient({ apiUrl: "https://example.test", getToken }));
    const settled = vi.fn();
    const read = client.listMessages(roomId, 0).catch(settled);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(getToken).toHaveBeenCalledTimes(1);
    expect(settled).toHaveBeenCalledWith(expect.objectContaining({ message: expect.stringMatching(/timed out/i) }));
    await read;
  } finally { vi.useRealTimers(); }
});

it("cancels history immediately and skips an already-cancelled read", async () => {
  const controller = new AbortController();
  const get = vi.fn(() => new Promise(() => {}));
  const client = createMessagingClient({ get } as unknown as ApiMethods);
  const read = client.listMessages(roomId, 0, 100, { signal: controller.signal });
  controller.abort();
  await expect(read).rejects.toThrow("cancelled");
  await expect(client.listMessages(roomId, 0, 100, { signal: controller.signal })).rejects.toThrow("cancelled");
  expect(get).toHaveBeenCalledTimes(1);
});

it("keeps a running session's live terminal coming by posting /viewing", async () => {
  const post = vi.fn(async () => undefined);
  const client = createMessagingClient({ post } as unknown as ApiMethods);
  await client.watchLocalSession("s 1");
  expect(post).toHaveBeenCalledWith("/messaging/sessions/s%201/viewing", {});
  await client.watchLocalSession("s 1", true); // a view that just mounted
  expect(post).toHaveBeenLastCalledWith("/messaging/sessions/s%201/viewing", { fresh: true });
});

it("posts an admitted viewer's keystrokes with the screen epoch to /input", async () => {
  const post = vi.fn(async () => undefined);
  const client = createMessagingClient({ post } as unknown as ApiMethods);
  await client.sendLocalSessionInput("s 1", "aGk=", 3);
  expect(post).toHaveBeenCalledWith("/messaging/sessions/s%201/input", { data: "aGk=", epoch: 3 });
  // The panel's size, alone or with keys (owner ruling 2026-10-14).
  await client.sendLocalSessionInput("s 1", "", 3, { cols: 120, rows: 40, claim: true });
  expect(post).toHaveBeenLastCalledWith("/messaging/sessions/s%201/input", { epoch: 3, size: { cols: 120, rows: 40, claim: true } });
  await client.sendLocalSessionInput("s 1", "aGk=", 3, { cols: 120, rows: 40, claim: true });
  expect(post).toHaveBeenLastCalledWith("/messaging/sessions/s%201/input", { data: "aGk=", epoch: 3, size: { cols: 120, rows: 40, claim: true } });
});
