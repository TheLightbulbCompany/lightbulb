import { describe, expect, it, vi } from "vitest";

import type { ApiMethods } from "../api/client";
import { createMessagingClient } from "./client";

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
  it("uses room-scoped local resources and preserves command identity with escaped path segments", async () => {
    const { client, methods } = setup();
    const command = { requestId: "request-1", expectedGeneration: "generation-1", kind: "send" as const, text: "continue" };
    await client.listResources("room/a");
    await client.listLocalSessionEvents("room/a", "session/b", 3, 20);
    await client.submitLocalSessionCommand("room/a", "session/b", command);
    await client.getLocalSessionCommand("room/a", "session/b", "request/c");
    await client.setLocalSessionEnrollment({ deviceId: "mac-id", cliDeviceId: "cli-id", enabled: false, expectedRevision: "enrollment-revision" });
    expect(methods.get.mock.calls).toEqual([
      ["/messaging/rooms/room%2Fa/resources"],
      ["/messaging/rooms/room%2Fa/local-sessions/session%2Fb/events?afterSeq=3&limit=20"],
      ["/messaging/rooms/room%2Fa/local-sessions/session%2Fb/commands/request%2Fc"],
    ]);
    expect(methods.post).toHaveBeenCalledWith("/messaging/rooms/room%2Fa/local-sessions/session%2Fb/commands", command);
    expect(methods.put).toHaveBeenCalledWith("/desktop/local-sessions/enrollment", { deviceId: "mac-id", cliDeviceId: "cli-id", enabled: false, expectedRevision: "enrollment-revision" });
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
      [`/messaging/rooms/${roomId}/messages?afterSeq=42&limit=100`],
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
    expect(methods.post).toHaveBeenNthCalledWith(2, `/messaging/rooms/${roomId}/messages`, send);
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
});
