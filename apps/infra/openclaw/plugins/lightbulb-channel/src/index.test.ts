import { mkdtemp, readFile, rm } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { createIngressEffectOnce } from "openclaw/plugin-sdk/ingress-effect-once";
import { applyBasicWebhookRequestGuards, registerPluginHttpRoute } from "openclaw/plugin-sdk/webhook-ingress";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BODY_TIMEOUT_MS,
  CHANNEL_CONFIG_JSON_SCHEMA,
  DURABLE_FINAL_CAPABILITIES,
  EFFECT_ONCE,
  MAX_BODY_BYTES,
  MAX_HISTORY,
  MAX_PARTICIPANTS,
  MAX_TEXT_BYTES,
  ROUTE_PREFIX,
  UNKNOWN_SEND_RECONCILIATION_TIMEOUT_MS,
  buildOutboundBody,
  createInboundHandler,
  createOutbound,
  dispatchRoomEvent,
  listAccountIds,
  normalizeTarget,
  parseIngress,
  resolveAccount,
  resolveEnv,
  resolveOutboundEventId,
  sessionKeyFor,
  startGatewayAccount,
  toHistoryEntries,
  toReceipt,
  type GatewayDeps,
  type LightbulbRoomIngress,
  type LightbulbSdk,
} from "./channel.js";
import entry, {
  lightbulbChannelConfigSchema,
  lightbulbChannelPlugin,
  lightbulbMessageAdapter,
} from "./index.js";

const FRIDAY = "11111111-1111-4111-8111-111111111111";
const GAUGE = "22222222-2222-4222-8222-222222222222";
const ROOM = "33333333-3333-4333-8333-333333333333";
const MESSAGE = "44444444-4444-4444-8444-444444444444";
const HISTORY_MESSAGE = "55555555-5555-4555-8555-555555555555";
const ATTACHMENT = "66666666-6666-4666-8666-666666666666";
const HISTORY_ATTACHMENT = "77777777-7777-4777-8777-777777777777";
const WEBP_ATTACHMENT = "88888888-8888-4888-8888-888888888888";
const GIF_ATTACHMENT = "99999999-9999-4999-8999-999999999999";
const CREATED_AT = "2026-09-10T12:34:56.000Z";
const ENV = {
  ISOL8_API_URL: "https://api.flickmeon.ai/",
  ISOL8_OWNER_ID: "owner_123",
  OPENCLAW_HOOK_TOKEN: "hook-token",
};

function canonicalMessage(overrides: Record<string, unknown> = {}) {
  return {
    messageId: MESSAGE,
    roomId: ROOM,
    messageSeq: 2,
    roomRevision: 7,
    author: { kind: "human", id: "user_1", displayName: "Founder" },
    text: "@friday please investigate",
    mentions: [{ kind: "agent", id: FRIDAY }],
    replyToMessageId: null,
    threadRootMessageId: null,
    createdAt: CREATED_AT,
    runId: null,
    attachments: [],
    ...overrides,
  };
}

function ingress(overrides: Record<string, unknown> = {}): LightbulbRoomIngress {
  const message = canonicalMessage();
  return {
    version: 1,
    eventId: MESSAGE,
    targetAgentId: FRIDAY,
    eventKind: "user_request",
    room: { id: ROOM, name: "launch", kind: "channel", revision: 7 },
    sender: message.author,
    message,
    mentions: message.mentions,
    participants: [
      { kind: "human", id: "user_1", displayName: "Founder" },
      { kind: "human", id: "user_2", displayName: "Partner" },
      {
        kind: "agent",
        id: FRIDAY,
        displayName: "Friday",
        mentionHandle: "friday",
        listenMode: "active",
      },
      {
        kind: "agent",
        id: GAUGE,
        displayName: "Gauge",
        mentionHandle: "gauge",
        listenMode: "mentions",
      },
    ],
    recentHistory: [
      canonicalMessage({
        messageId: HISTORY_MESSAGE,
        messageSeq: 1,
        author: { kind: "human", id: "user_2", displayName: "Partner" },
        text: "The launch is down ten percent.",
        mentions: [],
      }),
    ],
    ...overrides,
  } as LightbulbRoomIngress;
}

function config() {
  return {
    agents: {
      defaults: { utilityModel: "anthropic/claude-haiku-4-5" },
      entries: {
        [FRIDAY]: {
          name: "Friday",
          params: { paperclip: { role: "ceo", title: "Chief Executive", capabilities: "Strategy" } },
        },
        [GAUGE]: { name: "Gauge" },
      },
    },
    session: { groupScope: "per-group" },
    channels: {
      defaults: {
        botLoopProtection: { enabled: true, windowMs: 60_000, maxEvents: 20, cooldownMs: 60_000 },
      },
      lightbulb: {
        enabled: true,
        accounts: { [FRIDAY]: { enabled: true }, [GAUGE]: { enabled: true } },
      },
    },
    bindings: [
      {
        agentId: FRIDAY,
        match: { channel: "lightbulb", accountId: FRIDAY, peer: { kind: "channel", id: "*" } },
      },
      {
        agentId: GAUGE,
        match: { channel: "lightbulb", accountId: GAUGE, peer: { kind: "channel", id: "*" } },
      },
    ],
  } as any;
}

function dispatchedIngress() {
  return {
    ingress: { admission: "dispatch" },
    senderAccess: {},
    routeAccess: {},
    commandAccess: {},
    activationAccess: {},
    state: {},
  } as any;
}

function inboundSdk(overrides: Partial<LightbulbSdk> = {}): LightbulbSdk {
  return {
    applyBasicWebhookRequestGuards: vi.fn(() => true) as any,
    readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: ingress() })) as any,
    resolveChannelInboundRouteEnvelope: vi.fn(({ accountId, peer }: any) => ({
      route: {
        agentId: accountId,
        accountId,
        sessionKey: sessionKeyFor(accountId, peer.id),
        dmScope: "main",
      },
      buildEnvelope: ({ body }: { body: string }) => `[Lightbulb] ${body}`,
    })) as any,
    resolveStableChannelMessageIngress: vi.fn(async () => dispatchedIngress()) as any,
    buildChannelInboundEventContext: vi.fn((params: any) => ({
      ...params.extra,
      Body: params.message.body,
      BodyForAgent: params.message.bodyForAgent,
      RawBody: params.message.rawBody,
      CommandBody: params.message.commandBody,
      InboundHistory: params.message.inboundHistory,
      InboundEventKind: params.message.inboundEventKind,
      SessionKey: params.route.dispatchSessionKey,
      WasMentioned: params.access.mentions.wasMentioned,
      CommandAuthorized: params.access.commands.authorized,
      ReplyToId: params.reply.replyToId,
      MessageThreadId: params.reply.messageThreadId,
      SenderId: params.sender.id,
    })) as any,
    createIngressEffectOnce: vi.fn(() => {
      const committed = new Set<string>();
      return {
        runOnce: async ({ eventId, effect, run }: any) => {
          const key = `${effect}:${eventId}`;
          if (committed.has(key)) return { kind: "replayed" as const };
          const value = await run();
          committed.add(key);
          return { kind: "executed" as const, value };
        },
      };
    }) as any,
    prepareSimpleCompletionModelForAgent: vi.fn(async () => ({
      model: { provider: "anthropic", id: "claude-haiku-4-5" },
      auth: { apiKey: "test", mode: "api-key" },
      selection: { provider: "anthropic", modelId: "claude-haiku-4-5", agentDir: "/tmp/friday" },
    })) as any,
    completeWithPreparedSimpleCompletionModel: vi.fn(async () => ({
      role: "assistant",
      content: [{ type: "text", text: "DISPATCH" }],
      stopReason: "stop",
    })) as any,
    ...overrides,
  };
}

function runtime(dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }))) {
  return { inbound: { dispatch, buildContext: vi.fn() } } as any;
}

function responseRecorder() {
  const headers = new Map<string, string>();
  const response = {
    statusCode: 200,
    body: "",
    setHeader(name: string, value: string) {
      headers.set(name.toLowerCase(), String(value));
    },
    getHeader(name: string) {
      return headers.get(name.toLowerCase());
    },
    end(value = "") {
      response.body += String(value);
    },
  };
  return response as typeof response & ServerResponse;
}

function request(method = "POST", contentType = "application/json") {
  return {
    method,
    headers: { "content-type": contentType },
    socket: { destroyed: false, writableEnded: false },
  } as unknown as IncomingMessage;
}

function handlerDeps(options: {
  event?: LightbulbRoomIngress;
  sdk?: LightbulbSdk;
  dispatch?: ReturnType<typeof vi.fn>;
} = {}) {
  const dispatch = options.dispatch ?? vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
  const sdk = options.sdk ?? inboundSdk({
    readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: options.event ?? ingress() })) as any,
  });
  return {
    deps: { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
    sdk,
    dispatch,
  };
}

function sendContext(overrides: Record<string, unknown> = {}) {
  return {
    cfg: config(),
    to: `lightbulb:${ROOM}`,
    text: "I found two failures.",
    accountId: FRIDAY,
    preparedMessageId: "prepared-1",
    ...overrides,
  } as any;
}

function unknownContext(overrides: Record<string, unknown> = {}) {
  return {
    cfg: config(),
    queueId: "queue-1",
    channel: "lightbulb",
    to: `lightbulb:${ROOM}`,
    accountId: FRIDAY,
    enqueuedAt: 1,
    retryCount: 0,
    payloads: [],
    ...overrides,
  } as any;
}

describe("strict package and channel configuration", () => {
  it("keeps manifest and runtime channel schemas identical", async () => {
    const manifest = JSON.parse(
      await readFile(new URL("../openclaw.plugin.json", import.meta.url), "utf8"),
    );
    expect(manifest).toMatchObject({
      id: "lightbulb",
      version: "0.1.0",
      channels: ["lightbulb"],
      configSchema: { type: "object", properties: {}, additionalProperties: false },
    });
    expect(manifest.channelConfigs.lightbulb.schema).toEqual(CHANNEL_CONFIG_JSON_SCHEMA);
    expect(entry.id).toBe("lightbulb");
    expect(lightbulbChannelPlugin.id).toBe("lightbulb");
  });

  it("accepts only enabled/accounts and lowercase canonical account keys", () => {
    expect(
      lightbulbChannelConfigSchema.runtime.safeParse({
        enabled: true,
        accounts: { [FRIDAY]: { enabled: true } },
      }).success,
    ).toBe(true);
    expect(lightbulbChannelConfigSchema.runtime.safeParse({ extra: true }).success).toBe(false);
    expect(
      lightbulbChannelConfigSchema.runtime.safeParse({ accounts: { Friday: { enabled: true } } })
        .success,
    ).toBe(false);
    expect(
      lightbulbChannelConfigSchema.runtime.safeParse({ accounts: { [FRIDAY]: { token: "no" } } })
        .success,
    ).toBe(false);
  });

  it("accepts the checked-in backend handoff fixture", async () => {
    const fixture = JSON.parse(
      await readFile(new URL("../fixtures/production.json", import.meta.url), "utf8"),
    );
    expect(lightbulbChannelConfigSchema.runtime.safeParse(fixture.channels.lightbulb).success).toBe(
      true,
    );
    expect(fixture.messages.groupChat).toEqual({
      historyLimit: 50,
      unmentionedInbound: "room_event",
      visibleReplies: "automatic",
    });
    expect(fixture.session.groupScope).toBe("per-group");
    expect(fixture.tools.sessions.visibility).toBe("agent");
    expect(fixture.tools.agentToAgent.enabled).toBe(false);
    expect(fixture.bindings).toHaveLength(2);
  });

  it("sorts configured accounts and resolves their enabled state", () => {
    expect(listAccountIds(config())).toEqual([FRIDAY, GAUGE]);
    expect(resolveAccount(config(), FRIDAY)).toEqual({
      accountId: FRIDAY,
      configured: true,
      enabled: true,
    });
    expect(resolveAccount(config(), "FRIDAY").configured).toBe(false);
  });
});

describe("routing and outbound adapter", () => {
  it("accepts only canonical room targets", () => {
    expect(normalizeTarget(`lightbulb:${ROOM}`)).toBe(ROOM);
    expect(normalizeTarget(ROOM)).toBe(ROOM);
    expect(normalizeTarget("lightbulb:not-a-room")).toBeUndefined();
    expect(
      normalizeTarget("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
    ).toBeUndefined();
  });

  it("fails closed when agent A selects sibling account B while preserving same-account routing", async () => {
    const route = await lightbulbChannelPlugin.messaging?.resolveOutboundSessionRoute?.({
      cfg: config(),
      agentId: FRIDAY,
      accountId: FRIDAY,
      target: `lightbulb:${ROOM}`,
    });
    expect(route).toMatchObject({
      sessionKey: `agent:${FRIDAY}:lightbulb:channel:${ROOM}`,
      baseSessionKey: `agent:${FRIDAY}:lightbulb:channel:${ROOM}`,
      recipientSessionExact: true,
      peer: { kind: "channel", id: ROOM },
      chatType: "channel",
    });
    expect(sessionKeyFor(FRIDAY, ROOM)).toBe(route?.sessionKey);
    expect(() =>
      lightbulbChannelPlugin.messaging?.resolveOutboundSessionRoute?.({
        cfg: config(),
        agentId: GAUGE,
        accountId: FRIDAY,
        target: ROOM,
      }),
    ).toThrow("lightbulb: outbound agent does not match the selected account");
  });

  it("prioritizes queue/part ids and otherwise uses the prepared id", () => {
    expect(resolveOutboundEventId(sendContext())).toBe("prepared-1");
    expect(
      resolveOutboundEventId(
        sendContext({ deliveryQueueId: "queue-9", deliveryPartIndex: 3, preparedMessageId: "ignored" }),
      ),
    ).toBe("queue-9:3");
    expect(resolveOutboundEventId(sendContext({ deliveryQueueId: "queue-9" }))).toBe("queue-9:0");
    expect(() => resolveOutboundEventId(sendContext({ preparedMessageId: undefined }))).toThrow(
      /stable host idempotency/,
    );
    expect(() =>
      resolveOutboundEventId(sendContext({ deliveryQueueId: "queue", deliveryPartIndex: -1 })),
    ).toThrow(/part index/);
  });

  it("builds the frozen owner-independent body with reply/thread and no mentions or run id", () => {
    const body = buildOutboundBody(
      sendContext({ replyToId: MESSAGE, threadId: HISTORY_MESSAGE, deliveryQueueId: "queue-2" }),
    );
    expect(body).toEqual({
      eventId: "queue-2:0",
      roomId: ROOM,
      agentId: FRIDAY,
      text: "I found two failures.",
      replyToMessageId: MESSAGE,
      threadRootMessageId: HISTORY_MESSAGE,
    });
    expect(body).not.toHaveProperty("mentions");
    expect(body).not.toHaveProperty("runId");
  });

  it("posts once-resolved owner/agent identity with hook auth", async () => {
    let envReads = 0;
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify({ messageId: "canonical-1", roomId: ROOM, createdAt: CREATED_AT }), {
        status: 201,
        headers: { "content-type": "application/json" },
      }),
    );
    const adapter = createOutbound({
      fetch,
      env: () => {
        envReads += 1;
        return ENV;
      },
    });
    const result = await adapter.sendText(sendContext());
    expect(envReads).toBe(1);
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe(
      "https://api.flickmeon.ai/api/v1/internal/messaging/agent-messages/owner_123",
    );
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer hook-token");
    expect(JSON.parse(String(init.body))).toEqual({
      eventId: "prepared-1",
      roomId: ROOM,
      agentId: FRIDAY,
      text: "I found two failures.",
      replyToMessageId: null,
      threadRootMessageId: null,
    });
    expect(result).toEqual({
      messageId: "canonical-1",
      receipt: {
        primaryPlatformMessageId: "canonical-1",
        platformMessageIds: ["canonical-1"],
        parts: [{ platformMessageId: "canonical-1", kind: "text", index: 0 }],
        sentAt: Date.parse(CREATED_AT),
      },
    });
  });

  it("names missing runtime configuration without exposing values", async () => {
    expect(() => resolveEnv({ ...ENV, OPENCLAW_HOOK_TOKEN: undefined })).toThrowError(
      expect.objectContaining({ name: "LightbulbConfigurationError" }),
    );
    const adapter = createOutbound({ fetch: vi.fn() as any, env: () => ({ ...ENV, ISOL8_OWNER_ID: "" }) });
    await expect(adapter.sendText(sendContext())).rejects.toThrow(/ISOL8_OWNER_ID/);
  });

  it("trims exactly one trailing API slash", () => {
    expect(resolveEnv(ENV).base).toBe("https://api.flickmeon.ai");
    expect(resolveEnv({ ...ENV, ISOL8_API_URL: "https://api.flickmeon.ai//" }).base).toBe(
      "https://api.flickmeon.ai/",
    );
  });

  it("rejects non-201 sends without reading or logging the response body", async () => {
    const response = new Response("credential-like-body", { status: 503 });
    const text = vi.spyOn(response, "text");
    const adapter = createOutbound({ fetch: vi.fn(async () => response), env: () => ENV });
    await expect(adapter.sendText(sendContext())).rejects.toThrow(
      "lightbulb: backend rejected the message (503)",
    );
    expect(text).not.toHaveBeenCalled();
  });

  it("requires the exact three-field backend receipt", () => {
    expect(toReceipt({ messageId: "m1", roomId: ROOM, createdAt: CREATED_AT }, ROOM)).toEqual({
      primaryPlatformMessageId: "m1",
      platformMessageIds: ["m1"],
      parts: [{ platformMessageId: "m1", kind: "text", index: 0 }],
      sentAt: Date.parse(CREATED_AT),
    });
    expect(() =>
      toReceipt({ messageId: "m1", roomId: ROOM, createdAt: CREATED_AT, extra: true }, ROOM),
    ).toThrow(/malformed/);
    expect(() => toReceipt({ messageId: "m1", roomId: ROOM, createdAt: "yesterday" }, ROOM)).toThrow(
      /malformed/,
    );
  });

  it.each([
    [200, { messageId: "m1", roomId: ROOM, createdAt: CREATED_AT }, "sent"],
    [404, { code: "MESSAGE_NOT_FOUND" }, "not_sent"],
    [503, { code: "RUNTIME_UNAVAILABLE" }, "unresolved"],
    [200, { messageId: "m1", roomId: "wrong", createdAt: CREATED_AT }, "unresolved"],
  ])("reconciles receipt status %s as %s", async (status, body, expected) => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }),
    );
    const result = await createOutbound({ fetch, env: () => ENV }).reconcileUnknownSend(
      unknownContext(),
    );
    expect(result.status).toBe(expected);
    expect(fetch.mock.calls[0]?.[0]).toContain("/receipts/queue-1%3A0");
    if (expected === "sent") {
      expect(result).toMatchObject({ messageId: "m1", receipt: { sentAt: Date.parse(CREATED_AT) } });
    }
  });

  it("bounds receipt body reads and maps aborts to unresolved", async () => {
    const controller = new AbortController();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(controller.signal);
    const bodyRead = vi.fn(
      () =>
        new Promise<never>((_resolve, reject) => {
          controller.signal.addEventListener("abort", () => reject(controller.signal.reason), { once: true });
        }),
    );
    const fetch = vi.fn(async () => ({ status: 200, json: bodyRead }) as Response);
    try {
      const reconciliation = createOutbound({ fetch, env: () => ENV }).reconcileUnknownSend(unknownContext());
      await vi.waitFor(() => expect(bodyRead).toHaveBeenCalledOnce());
      expect(timeout).toHaveBeenCalledWith(UNKNOWN_SEND_RECONCILIATION_TIMEOUT_MS);
      expect(fetch).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ signal: controller.signal }));

      controller.abort();

      await expect(reconciliation).resolves.toEqual({ status: "unresolved", retryable: true });
    } finally {
      timeout.mockRestore();
    }
  });

  it("leaves transport failures unresolved for native retry", async () => {
    const result = await createOutbound({
      fetch: vi.fn(async () => {
        throw new Error("network down");
      }),
      env: () => ENV,
    }).reconcileUnknownSend(unknownContext());
    expect(result).toEqual({ status: "unresolved", retryable: true });
  });

  it("declares exact durable-final and reconciliation capabilities", () => {
    expect(lightbulbMessageAdapter.durableFinal).toMatchObject({
      capabilities: DURABLE_FINAL_CAPABILITIES,
      automaticUnknownSendReconciliation: true,
      reconcileUnknownSendKinds: { text: true },
    });
    expect(lightbulbMessageAdapter.send?.text).toBeTypeOf("function");
  });
});

describe("strict LightbulbRoomIngress", () => {
  it("accepts structured multi-human context and exact prior history", () => {
    const parsed = parseIngress(ingress());
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.value.participants.map((participant) => participant.displayName)).toEqual([
      "Founder",
      "Partner",
      "Friday",
      "Gauge",
    ]);
    expect(toHistoryEntries(parsed.value.recentHistory)).toEqual([
      {
        sender: "Partner",
        body: "The launch is down ten percent.",
        timestamp: Date.parse(CREATED_AT),
        messageId: HISTORY_MESSAGE,
      },
    ]);
  });

  it.each([
    ["unknown top-level field", { extra: true }],
    ["unknown event kind", { eventKind: "timer" }],
    ["uppercase target account", { targetAgentId: "Friday" }],
    ["malformed room id", { room: { id: "room-1", name: null, kind: "channel", revision: 1 } }],
    ["absent target participant", { participants: [{ kind: "human", id: "user_1", displayName: "Founder" }] }],
  ])("rejects %s", (_name, patch) => {
    expect(parseIngress(ingress(patch)).ok).toBe(false);
  });

  it("rejects sender/message identity drift and mention drift", () => {
    expect(
      parseIngress(ingress({ sender: { kind: "human", id: "user_2", displayName: "Partner" } })).ok,
    ).toBe(false);
    expect(parseIngress(ingress({ mentions: [] })).ok).toBe(false);
  });

  it("rejects oversized text, history, participants, and non-prior history", () => {
    expect(
      parseIngress(
        ingress({ message: canonicalMessage({ text: "x".repeat(MAX_TEXT_BYTES + 1) }) }),
      ).ok,
    ).toBe(false);
    expect(
      parseIngress(
        ingress({ recentHistory: Array.from({ length: MAX_HISTORY + 1 }, () => canonicalMessage()) }),
      ).ok,
    ).toBe(false);
    expect(
      parseIngress(
        ingress({ participants: Array.from({ length: MAX_PARTICIPANTS + 1 }, () => ingress().participants[0]) }),
      ).ok,
    ).toBe(false);
    expect(
      parseIngress(
        ingress({ recentHistory: [canonicalMessage({ messageSeq: 2, messageId: HISTORY_MESSAGE })] }),
      ).ok,
    ).toBe(false);
  });

  it("defaults missing attachments to empty and accepts bounded raster metadata", () => {
    const withoutAttachments = parseIngress(ingress());
    expect(withoutAttachments.ok && withoutAttachments.value.message.attachments).toEqual([]);

    const parsed = parseIngress(ingress({
      message: canonicalMessage({
        attachments: [{ id: ATTACHMENT, fileName: "launch.png", contentType: "image/png", size: 5 * 1024 * 1024 }],
      }),
    }));
    expect(parsed.ok && parsed.value.message.attachments).toEqual([
      { id: ATTACHMENT, fileName: "launch.png", contentType: "image/png", size: 5 * 1024 * 1024 },
    ]);
  });

  it.each([
    ["client path", { id: ATTACHMENT, fileName: "launch.png", contentType: "image/png", size: 1, path: "/tmp/x" }],
    ["invalid id", { id: "attachment-1", fileName: "launch.png", contentType: "image/png", size: 1 }],
    ["unsafe filename", { id: ATTACHMENT, fileName: "../launch.png", contentType: "image/png", size: 1 }],
    ["unsupported MIME", { id: ATTACHMENT, fileName: "launch.svg", contentType: "image/svg+xml", size: 1 }],
    ["oversized file", { id: ATTACHMENT, fileName: "launch.png", contentType: "image/png", size: 5 * 1024 * 1024 + 1 }],
  ])("rejects attachment %s", (_name, attachment) => {
    expect(parseIngress(ingress({ message: canonicalMessage({ attachments: [attachment] }) })).ok).toBe(false);
  });

  it("rejects more than four attachments", () => {
    const attachment = { id: ATTACHMENT, fileName: "launch.png", contentType: "image/png", size: 1 };
    expect(parseIngress(ingress({ message: canonicalMessage({ attachments: Array(5).fill(attachment) }) })).ok).toBe(false);
  });
});

describe("native inbound context and delivery", () => {
  it("uses the utility model to consume irrelevant ambient messages without a full agent turn", async () => {
    const prepare = vi.fn(async () => ({
      model: { provider: "anthropic", id: "claude-haiku-4-5" },
      auth: { apiKey: "test", mode: "api-key" },
      selection: { provider: "anthropic", modelId: "claude-haiku-4-5", agentDir: "/tmp/friday" },
    }));
    const complete = vi.fn(async () => ({
      role: "assistant",
      content: [{ type: "text", text: "IGNORE" }],
      stopReason: "stop",
    }));
    const sdk = inboundSdk({
      prepareSimpleCompletionModelForAgent: prepare,
      completeWithPreparedSimpleCompletionModel: complete,
    } as any);
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const message = canonicalMessage({ mentions: [], text: "Lunch arrived in the kitchen." });

    const outcome = await dispatchRoomEvent(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress({ eventKind: "room_event", message, mentions: [] }),
    );

    expect(outcome).toBe("ignored");
    expect(prepare).toHaveBeenCalledWith(expect.objectContaining({ agentId: FRIDAY, useUtilityModel: true }));
    expect(complete).toHaveBeenCalledWith(expect.objectContaining({
      options: expect.objectContaining({ maxTokens: 16, temperature: 0, reasoning: "off" }),
    }));
    const prompt = JSON.parse((complete.mock.calls[0]![0] as any).context.messages[0].content);
    expect(prompt.agent).toEqual({
      name: "Friday",
      role: "ceo",
      title: "Chief Executive",
      capabilities: "Strategy",
    });
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("fails closed when the ambient utility model does not return a supported decision", async () => {
    const sdk = inboundSdk({
      prepareSimpleCompletionModelForAgent: vi.fn(async () => ({
        model: { provider: "anthropic", id: "claude-haiku-4-5" },
        auth: { apiKey: "test", mode: "api-key" },
        selection: { provider: "anthropic", modelId: "claude-haiku-4-5", agentDir: "/tmp/friday" },
      })),
      completeWithPreparedSimpleCompletionModel: vi.fn(async () => ({
        role: "assistant",
        content: [{ type: "text", text: "maybe" }],
        stopReason: "stop",
      })),
    } as any);
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const message = canonicalMessage({ mentions: [], text: "Ambient update." });

    const outcome = await dispatchRoomEvent(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress({ eventKind: "room_event", message, mentions: [] }),
    );

    expect(outcome).toBe("gate_error");
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("fails closed instead of falling back to the primary model when no utility model is configured", async () => {
    const prepare = vi.fn();
    const sdk = inboundSdk({ prepareSimpleCompletionModelForAgent: prepare } as any);
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const cfg = config();
    delete cfg.agents.defaults.utilityModel;

    const outcome = await dispatchRoomEvent(
      { sdk, cfg, accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress({ eventKind: "room_event", message: canonicalMessage({ mentions: [] }), mentions: [] }),
    );

    expect(outcome).toBe("gate_error");
    expect(prepare).not.toHaveBeenCalled();
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("fails closed when an agent disables the configured default utility model", async () => {
    const prepare = vi.fn();
    const sdk = inboundSdk({ prepareSimpleCompletionModelForAgent: prepare } as any);
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const cfg = config();
    cfg.agents.entries[FRIDAY].utilityModel = "";

    const outcome = await dispatchRoomEvent(
      { sdk, cfg, accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress({ eventKind: "room_event", message: canonicalMessage({ mentions: [] }), mentions: [] }),
    );

    expect(outcome).toBe("gate_error");
    expect(prepare).not.toHaveBeenCalled();
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("forwards only current attachments as native media under the trusted OpenClaw media root", async () => {
    const sdk = inboundSdk();
    const current = [
      { id: ATTACHMENT, fileName: "launch source.png", contentType: "image/png", size: 123 },
      { id: HISTORY_ATTACHMENT, fileName: "photo.jpeg", contentType: "image/jpeg", size: 124 },
      { id: WEBP_ATTACHMENT, fileName: "chart.webp", contentType: "image/webp", size: 125 },
      { id: GIF_ATTACHMENT, fileName: "demo.gif", contentType: "image/gif", size: 126 },
    ];
    const historical = { id: MESSAGE, fileName: "old.gif", contentType: "image/gif", size: 456 };
    const event = ingress({
      message: canonicalMessage({ attachments: current }),
      recentHistory: [canonicalMessage({
        messageId: HISTORY_MESSAGE,
        messageSeq: 1,
        attachments: [historical],
      })],
    });

    await dispatchRoomEvent(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV },
      event,
    );

    const contextInput = vi.mocked(sdk.buildChannelInboundEventContext).mock.calls[0]![0] as any;
    expect(contextInput.media).toEqual([
      expect.objectContaining({ path: `/home/node/.openclaw/media/messaging/attachments/${ROOM}/${ATTACHMENT}.png` }),
      expect.objectContaining({ path: `/home/node/.openclaw/media/messaging/attachments/${ROOM}/${HISTORY_ATTACHMENT}.jpg` }),
      expect.objectContaining({ path: `/home/node/.openclaw/media/messaging/attachments/${ROOM}/${WEBP_ATTACHMENT}.webp` }),
      expect.objectContaining({ path: `/home/node/.openclaw/media/messaging/attachments/${ROOM}/${GIF_ATTACHMENT}.gif` }),
    ]);
    expect(contextInput.media[0]).toMatchObject({
      contentType: "image/png", kind: "image", fileName: "launch source.png", sizeBytes: 123, messageId: MESSAGE,
    });
    expect(JSON.stringify(contextInput.media)).not.toContain(`${MESSAGE}.gif`);
  });

  it("passes explicit open admission, event kind, mentions, and structured context", async () => {
    const sdk = inboundSdk();
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    await dispatchRoomEvent(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress(),
    );
    expect(sdk.resolveStableChannelMessageIngress).toHaveBeenCalledWith(
      expect.objectContaining({
        channelId: "lightbulb",
        accountId: FRIDAY,
        groupPolicy: "open",
        contextBinding: expect.objectContaining({
          agentId: FRIDAY,
          sessionKey: sessionKeyFor(FRIDAY, ROOM),
          messageId: MESSAGE,
          inboundEventKind: "user_request",
        }),
        mentionFacts: expect.objectContaining({ wasMentioned: true }),
      }),
    );
    const contextInput = vi.mocked(sdk.buildChannelInboundEventContext).mock.calls[0]![0] as any;
    expect(contextInput.message.inboundEventKind).toBe("user_request");
    expect(contextInput.message.bodyForAgent).toBe("@friday please investigate");
    expect(contextInput.message.body).toContain("@friday please investigate");
    expect(contextInput.message.inboundHistory).toEqual([
      {
        sender: "Partner",
        body: "The launch is down ten percent.",
        timestamp: Date.parse(CREATED_AT),
        messageId: HISTORY_MESSAGE,
      },
    ]);
    expect(contextInput.extra.ChannelStructuredContext[0].payload.participants).toHaveLength(4);
    expect(contextInput.access).toMatchObject({
      commands: { authorized: true },
      mentions: { wasMentioned: true, explicitlyMentionedBot: true },
    });
    expect(sdk.prepareSimpleCompletionModelForAgent).not.toHaveBeenCalled();
    expect(sdk.completeWithPreparedSimpleCompletionModel).not.toHaveBeenCalled();
  });

  it("marks ambient agent ingress non-command and supplies symmetric native pair-loop facts", async () => {
    const sdk = inboundSdk();
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const peerMessage = canonicalMessage({
      author: { kind: "agent", id: GAUGE, displayName: "Gauge" },
      mentions: [],
      text: "/status should remain conversation text",
    });
    const event = ingress({
      eventKind: "room_event",
      sender: peerMessage.author,
      message: peerMessage,
      mentions: [],
    });
    await dispatchRoomEvent(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      event,
    );
    expect(sdk.resolveStableChannelMessageIngress).toHaveBeenCalledWith(
      expect.objectContaining({ command: false, contextBinding: expect.objectContaining({ inboundEventKind: "room_event" }) }),
    );
    const contextInput = vi.mocked(sdk.buildChannelInboundEventContext).mock.calls[0]![0] as any;
    expect(contextInput.access.commands.authorized).toBe(false);
    expect(contextInput.extra.CommandInterpretationSuppressed).toBe(true);
    expect(dispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        botLoopProtection: {
          scopeId: "lightbulb:owner_123",
          conversationId: ROOM,
          senderId: GAUGE,
          receiverId: FRIDAY,
          eventId: MESSAGE,
          defaultsConfig: config().channels.defaults.botLoopProtection,
          defaultEnabled: true,
        },
      }),
    );

    const reverseDispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const reverseMessage = canonicalMessage({
      author: { kind: "agent", id: FRIDAY, displayName: "Friday" },
      mentions: [],
    });
    await dispatchRoomEvent(
      { sdk: inboundSdk(), cfg: config(), accountId: GAUGE, channelRuntime: runtime(reverseDispatch), env: () => ENV },
      ingress({ targetAgentId: GAUGE, sender: reverseMessage.author, message: reverseMessage, mentions: [] }),
    );
    expect(reverseDispatch.mock.calls[0]?.[0].botLoopProtection).toMatchObject({
      scopeId: "lightbulb:owner_123",
      senderId: FRIDAY,
      receiverId: GAUGE,
    });
  });

  it("durably admits only user-request finals and makes direct fallback fail closed", async () => {
    let delivered: unknown;
    const dispatch = vi.fn(async (params: any) => {
      const final = { text: "addressed final" };
      delivered = await params.delivery.preparePayload(final, { kind: "final" });
      expect(params.delivery.preparePayload({ text: "block" }, { kind: "block" })).toBeNull();
      expect(params.delivery.durable).toEqual({
        to: `lightbulb:${ROOM}`,
        replyToId: null,
        threadId: undefined,
        requiredCapabilities: DURABLE_FINAL_CAPABILITIES,
      });
      await expect(params.delivery.deliver(final, { kind: "final" })).rejects.toThrow(
        /direct delivery is not supported/,
      );
      return { admission: { kind: "dispatch" }, dispatched: true };
    });
    await dispatchRoomEvent(
      { sdk: inboundSdk(), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress(),
    );
    expect(delivered).toEqual({ text: "addressed final" });
  });

  it("auto-delivers finals after the ambient gate approves without authorizing commands", async () => {
    const dispatch = vi.fn(async (params: any) => {
      expect(params.ctxPayload.InboundEventKind).toBe("user_request");
      expect(params.ctxPayload.CommandAuthorized).toBe(false);
      expect(params.delivery.preparePayload({ text: "ambient final" }, { kind: "final" })).toEqual({
        text: "ambient final",
      });
      expect(params.delivery.durable).toEqual({
        to: `lightbulb:${ROOM}`,
        replyToId: null,
        threadId: undefined,
        requiredCapabilities: DURABLE_FINAL_CAPABILITIES,
      });
      return { admission: { kind: "dispatch" }, dispatched: true };
    });
    await dispatchRoomEvent(
      { sdk: inboundSdk(), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV },
      ingress({ eventKind: "room_event" }),
    );
    expect(dispatch).toHaveBeenCalledOnce();
    expect(lightbulbMessageAdapter.send?.text).toBeTypeOf("function");
  });

  it("maps native admission refusal to a rejected turn", async () => {
    const sdk = inboundSdk({
      resolveStableChannelMessageIngress: vi.fn(async () => ({
        ...dispatchedIngress(),
        ingress: { admission: "deny" },
      })) as any,
    });
    await expect(
      dispatchRoomEvent(
        { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV },
        ingress(),
      ),
    ).rejects.toMatchObject({ status: 409 });
  });

  it("refuses a route that does not bind the target account", async () => {
    const sdk = inboundSdk({
      resolveChannelInboundRouteEnvelope: vi.fn(() => ({
        route: { agentId: GAUGE, accountId: FRIDAY, sessionKey: sessionKeyFor(GAUGE, ROOM) },
        buildEnvelope: vi.fn(),
      })) as any,
    });
    await expect(
      dispatchRoomEvent(
        { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV },
        ingress(),
      ),
    ).rejects.toMatchObject({ status: 500 });
  });
});

describe("private ingress handler", () => {
  it("returns a consumed ignored status for an ambient gate decline", async () => {
    const event = ingress({
      eventKind: "room_event",
      message: canonicalMessage({ mentions: [], text: "Lunch arrived in the kitchen." }),
      mentions: [],
    });
    const sdk = inboundSdk({
      readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: event })),
      prepareSimpleCompletionModelForAgent: vi.fn(async () => ({
        model: { provider: "anthropic", id: "claude-haiku-4-5" },
        auth: { apiKey: "test", mode: "api-key" },
        selection: { provider: "anthropic", modelId: "claude-haiku-4-5", agentDir: "/tmp/friday" },
      })),
      completeWithPreparedSimpleCompletionModel: vi.fn(async () => ({
        role: "assistant",
        content: [{ type: "text", text: "IGNORE" }],
        stopReason: "stop",
      })),
    } as any);
    const state = handlerDeps({ sdk });
    const res = responseRecorder();

    await createInboundHandler(state.deps)(request(), res);

    expect(res.statusCode).toBe(202);
    expect(JSON.parse(res.body).status).toBe("ignored");
    expect(state.dispatch).not.toHaveBeenCalled();
  });

  it("applies the exact body guard contract before parsing", async () => {
    const guard = vi.fn(() => true);
    const read = vi.fn(async () => ({ ok: false as const }));
    const sdk = inboundSdk({
      applyBasicWebhookRequestGuards: guard as any,
      readJsonWebhookBodyOrReject: read as any,
    });
    const res = responseRecorder();
    await createInboundHandler(handlerDeps({ sdk }).deps)(request(), res);
    expect(guard).toHaveBeenCalledWith(
      expect.objectContaining({ allowMethods: ["POST"], requireJsonContentType: true }),
    );
    expect(read).toHaveBeenCalledWith(
      expect.objectContaining({ maxBytes: MAX_BODY_BYTES, timeoutMs: BODY_TIMEOUT_MS }),
    );
  });

  it("preserves native 405/415 guards", () => {
    const methodRes = responseRecorder();
    expect(
      applyBasicWebhookRequestGuards({
        req: request("GET"),
        res: methodRes,
        allowMethods: ["POST"],
        requireJsonContentType: true,
      }),
    ).toBe(false);
    expect(methodRes.statusCode).toBe(405);
    expect(methodRes.getHeader("allow")).toBe("POST");

    const typeRes = responseRecorder();
    expect(
      applyBasicWebhookRequestGuards({
        req: request("POST", "text/plain"),
        res: typeRes,
        allowMethods: ["POST"],
        requireJsonContentType: true,
      }),
    ).toBe(false);
    expect(typeRes.statusCode).toBe(415);
  });

  it.each([413, 408])("preserves body helper status %s", async (status) => {
    const sdk = inboundSdk({
      readJsonWebhookBodyOrReject: vi.fn(async ({ res }: any) => {
        res.statusCode = status;
        res.end("rejected");
        return { ok: false };
      }) as any,
    });
    const res = responseRecorder();
    await createInboundHandler(handlerDeps({ sdk }).deps)(request(), res);
    expect(res.statusCode).toBe(status);
  });

  it("rejects unknown target accounts and self echoes before native dispatch", async () => {
    const unknown = handlerDeps({ event: ingress({ targetAgentId: GAUGE }) });
    const unknownRes = responseRecorder();
    await createInboundHandler(unknown.deps)(request(), unknownRes);
    expect(unknownRes.statusCode).toBe(400);
    expect(unknown.dispatch).not.toHaveBeenCalled();

    const selfMessage = canonicalMessage({
      author: { kind: "agent", id: FRIDAY, displayName: "Friday" },
      mentions: [],
    });
    const self = handlerDeps({
      event: ingress({ sender: selfMessage.author, message: selfMessage, mentions: [] }),
    });
    const selfRes = responseRecorder();
    await createInboundHandler(self.deps)(request(), selfRes);
    expect(selfRes.statusCode).toBe(409);
    expect(self.dispatch).not.toHaveBeenCalled();
  });

  it("commits native admission refusals before returning 409", async () => {
    const resolveIngress = vi.fn(async () => ({
      ...dispatchedIngress(),
      ingress: { admission: "deny" },
    }));
    const event = ingress();
    const sdk = inboundSdk({
      readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: event })),
      resolveStableChannelMessageIngress: resolveIngress,
    } as any);
    const handler = createInboundHandler(handlerDeps({ sdk }).deps);
    const first = responseRecorder();
    const replay = responseRecorder();

    await handler(request(), first);
    await handler(request(), replay);

    expect(first.statusCode).toBe(409);
    expect(JSON.parse(first.body).error).toBe("not_admitted");
    expect(replay.statusCode).toBe(202);
    expect(JSON.parse(replay.body).status).toBe("replayed");
    expect(resolveIngress).toHaveBeenCalledOnce();
  });

  it("deduplicates a committed canonical event id", async () => {
    const state = handlerDeps();
    const handler = createInboundHandler(state.deps);
    const first = responseRecorder();
    const replay = responseRecorder();
    await handler(request(), first);
    await handler(request(), replay);
    expect(first.statusCode).toBe(202);
    expect(JSON.parse(first.body).status).toBe("dispatched");
    expect(JSON.parse(replay.body).status).toBe("replayed");
    expect(state.dispatch).toHaveBeenCalledOnce();
    expect(state.sdk.createIngressEffectOnce).toHaveBeenCalledWith({
      pluginId: "lightbulb",
      namespacePrefix: `lightbulb-inbound-${FRIDAY}`,
      ...EFFECT_ONCE,
    });
  });

  it("answers 202 as soon as the turn is admitted, before the SDK turn resolves", async () => {
    let finishTurn: (value: unknown) => void = () => {};
    const dispatch = vi.fn(() => new Promise((resolve) => { finishTurn = resolve; }));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const res = responseRecorder();
    await createInboundHandler(handlerDeps({ dispatch }).deps)(request(), res);
    expect(res.statusCode).toBe(202);
    expect(JSON.parse(res.body).status).toBe("dispatched");
    expect(dispatch).toHaveBeenCalledTimes(1);
    finishTurn({ admission: { kind: "dispatch" }, dispatched: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("logs, without retry or content, a turn the SDK declines or drops after the ack", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const declined = responseRecorder();
    await createInboundHandler(
      handlerDeps({ dispatch: vi.fn(async () => ({ admission: { kind: "drop", reason: "loop" }, dispatched: false })) }).deps,
    )(request(), declined);
    expect(declined.statusCode).toBe(202);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(warn).toHaveBeenCalledWith("[lightbulb-channel] turn declined after ack", expect.objectContaining({ admission: "drop", reason: "loop" }));

    const failed = responseRecorder();
    await createInboundHandler(
      handlerDeps({ dispatch: vi.fn(async () => { throw new Error("gateway exploded: secret text"); }) }).deps,
    )(request(), failed);
    expect(failed.statusCode).toBe(202);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(warn).toHaveBeenCalledWith("[lightbulb-channel] turn failed after ack", { eventId: expect.any(String), error: "Error" });
    expect(JSON.stringify(warn.mock.calls)).not.toContain("secret text");
    warn.mockRestore();
  });

  it("documents the at-least-once crash window after dispatch and before commit", async () => {
    let crash = true;
    const crashGuard = vi.fn(() => ({
      runOnce: async ({ run }: any) => {
        const value = await run();
        if (crash) throw new Error("simulated process death before persistent commit");
        return { kind: "executed" as const, value };
      },
    }));
    const sdk = inboundSdk({ createIngressEffectOnce: crashGuard as any });
    const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
    const first = responseRecorder();
    await createInboundHandler(handlerDeps({ sdk, dispatch }).deps)(request(), first);
    expect(first.statusCode).toBe(500);
    crash = false;
    const retry = responseRecorder();
    await createInboundHandler(handlerDeps({ sdk, dispatch }).deps)(request(), retry);
    expect(retry.statusCode).toBe(202);
    expect(dispatch).toHaveBeenCalledTimes(2);
  });
});

describe.sequential("persistent ingress effect guard", () => {
  const oldStateDir = process.env.OPENCLAW_STATE_DIR;
  let stateDir = "";

  afterEach(async () => {
    if (oldStateDir === undefined) delete process.env.OPENCLAW_STATE_DIR;
    else process.env.OPENCLAW_STATE_DIR = oldStateDir;
    if (stateDir) await rm(stateDir, { recursive: true, force: true });
    stateDir = "";
  });

  it("replays a committed event after recreating the account-scoped guard", async () => {
    stateDir = await mkdtemp(path.join(tmpdir(), "lightbulb-ingress-"));
    process.env.OPENCLAW_STATE_DIR = stateDir;
    let calls = 0;
    const options = {
      pluginId: "lightbulb",
      namespacePrefix: `lightbulb-inbound-${FRIDAY}-${Date.now()}`,
      ...EFFECT_ONCE,
    };
    const first = await createIngressEffectOnce(options).runOnce({
      eventId: MESSAGE,
      effect: "dispatch",
      run: async () => ++calls,
    });
    const replay = await createIngressEffectOnce(options).runOnce({
      eventId: MESSAGE,
      effect: "dispatch",
      run: async () => ++calls,
    });
    expect(first).toEqual({ kind: "executed", value: 1 });
    expect(replay).toEqual({ kind: "replayed" });
    expect(calls).toBe(1);
  });

  it("releases a pre-effect failure for retry", async () => {
    stateDir = await mkdtemp(path.join(tmpdir(), "lightbulb-ingress-"));
    process.env.OPENCLAW_STATE_DIR = stateDir;
    let calls = 0;
    const guard = createIngressEffectOnce({
      pluginId: "lightbulb",
      namespacePrefix: `lightbulb-inbound-${FRIDAY}-${Date.now()}`,
      ...EFFECT_ONCE,
    });
    await expect(
      guard.runOnce({
        eventId: MESSAGE,
        effect: "dispatch",
        run: async () => {
          calls += 1;
          throw new Error("before effect completed");
        },
      }),
    ).rejects.toThrow(/before effect/);
    expect(
      await guard.runOnce({ eventId: MESSAGE, effect: "dispatch", run: async () => ++calls }),
    ).toEqual({ kind: "executed", value: 2 });
  });
});

describe("per-agent gateway lifecycle", () => {
  function lifecycleDeps(registrations: any[]): GatewayDeps {
    return {
      sdk: inboundSdk(),
      env: () => ENV,
      registerPluginHttpRoute: vi.fn((registration: any) => {
        registrations.push(registration);
        return () => {
          registration.unregistered = true;
        };
      }) as any,
      waitUntilAbort: ((signal?: AbortSignal) =>
        new Promise<void>((resolve) => {
          if (signal?.aborted) resolve();
          else signal?.addEventListener("abort", () => resolve(), { once: true });
        })) as any,
      channelReadyPatch: ((extras: any) => ({
        running: true,
        connected: true,
        lifecycle: "ready",
        lastConnectedAt: 1,
        lastError: null,
        ...extras,
      })) as any,
      channelStoppedPatch: ((extras: any) => ({
        running: false,
        connected: false,
        lifecycle: "stopped",
        ...extras,
      })) as any,
    };
  }

  function lifecycleContext(accountId: string, controller: AbortController, statuses: any[]) {
    return {
      cfg: config(),
      accountId,
      account: resolveAccount(config(), accountId),
      abortSignal: controller.signal,
      channelRuntime: runtime(),
      runtime: {} as any,
      getStatus: () => statuses.at(-1),
      setStatus: (status: any) => statuses.push(status),
    } as any;
  }

  it("registers two exact unique gateway-auth routes and cleans both up", async () => {
    const registrations: any[] = [];
    const deps = lifecycleDeps(registrations);
    const fridayAbort = new AbortController();
    const gaugeAbort = new AbortController();
    const fridayStatuses: any[] = [];
    const gaugeStatuses: any[] = [];
    const friday = startGatewayAccount(lifecycleContext(FRIDAY, fridayAbort, fridayStatuses), deps);
    const gauge = startGatewayAccount(lifecycleContext(GAUGE, gaugeAbort, gaugeStatuses), deps);

    expect(registrations.map((row) => row.path)).toEqual([
      `${ROUTE_PREFIX}${FRIDAY}`,
      `${ROUTE_PREFIX}${GAUGE}`,
    ]);
    for (const row of registrations) {
      expect(row).toMatchObject({
        auth: "gateway",
        match: "exact",
        pluginId: "lightbulb",
        source: "lightbulb-channel-gateway",
        replaceExisting: true,
        throwOnFailure: true,
      });
    }
    expect(fridayStatuses.map((status) => status.lifecycle)).toEqual(["starting", "ready"]);
    expect(gaugeStatuses.map((status) => status.lifecycle)).toEqual(["starting", "ready"]);

    fridayAbort.abort();
    gaugeAbort.abort();
    await Promise.all([friday, gauge]);
    expect(registrations.every((row) => row.unregistered)).toBe(true);
    expect(fridayStatuses.at(-1).lifecycle).toBe("stopped");
    expect(gaugeStatuses.at(-1).lifecycle).toBe("stopped");
  });

  it("fails closed when an unrelated owner already holds the exact route", () => {
    const registry = { httpRoutes: [] } as any;
    const path = `${ROUTE_PREFIX}${FRIDAY}`;
    const unregister = registerPluginHttpRoute({
      path,
      auth: "gateway",
      match: "exact",
      pluginId: "other",
      source: "other-source",
      handler: () => true,
      registry,
      throwOnFailure: true,
    });
    expect(() =>
      registerPluginHttpRoute({
        path,
        auth: "gateway",
        match: "exact",
        pluginId: "lightbulb",
        source: "lightbulb-channel-gateway",
        accountId: FRIDAY,
        replaceExisting: true,
        handler: () => true,
        registry,
        throwOnFailure: true,
      }),
    ).toThrow(/replacement denied/);
    unregister();
  });
});
