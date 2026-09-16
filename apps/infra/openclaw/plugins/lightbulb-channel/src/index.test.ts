import { mkdtemp, readFile, rm } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import { createIngressEffectOnce } from "openclaw/plugin-sdk/ingress-effect-once";
import { buildChannelInboundEventContext } from "openclaw/plugin-sdk/channel-inbound";
import { redactSensitiveText } from "openclaw/plugin-sdk/logging-core";
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
  RUNS_ROUTE_PREFIX,
  UNKNOWN_SEND_RECONCILIATION_TIMEOUT_MS,
  admitRoomEvent,
  buildOutboundBody,
  createInboundHandler,
  createRunControlHandler,
  createOutbound,
  listAccountIds,
  normalizeTarget,
  parseIngress,
  resolveAccount,
  resolveEnv,
  resolveOutboundEventId,
  runRoomTurn,
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
const WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const DELIVERY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const RUN = {
  runId: RUN_ID,
  workspaceId: WORKSPACE,
  frameChannel: `/ws/${WORKSPACE}/rooms/${ROOM}/${DELIVERY}`,
  frameEndpoint: "https://abc123.appsync-api.us-east-1.amazonaws.com/event",
};
const OTHER_MESSAGE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const OTHER_RUN = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const ENV = {
  ISOL8_API_URL: "https://api.flickmeon.ai/",
  ISOL8_OWNER_ID: "owner_123",
  OPENCLAW_HOOK_TOKEN: "hook-token",
};

/** One-line replacement for the retired `dispatchRoomEvent`: admit, then run the turn. */
async function dispatchTurn(
  deps: Parameters<typeof admitRoomEvent>[0],
  event: LightbulbRoomIngress,
  invocation?: string,
) {
  return runRoomTurn(deps, event, await admitRoomEvent(deps, event), invocation);
}

/** An inert supervisor: `start` never runs the turn, so acks are observed on their own. */
function fakeRuns() {
  return { start: vi.fn(), abort: vi.fn(() => "aborting" as const), steer: vi.fn(() => "accepted" as const), runIdForSession: vi.fn(() => RUN_ID) };
}

/** A supervisor that does run the background turn, for the tests that assert turn behaviour. */
function turnRuns() {
  const turns: Promise<unknown>[] = [];
  return Object.assign(fakeRuns(), {
    start: vi.fn((_spec: unknown, turn: () => Promise<unknown>) => {
      turns.push(turn().catch((error) => error));
    }),
    /** Resolves to every finished turn's outcome, or the error it failed with. */
    settled: () => Promise.all(turns),
  });
}

function withBody(body: unknown, overrides: Partial<LightbulbSdk> = {}) {
  return inboundSdk({ readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: body })) as any, ...overrides });
}

it("carries invocation sentinel only in the actual SDK runtime binding, never prompt fields or another ingress", async () => {
  const now = Math.floor(Date.now() / 1000);
  const token = `eyJhbGciOiJIUzI1NiJ9.${Buffer.from(JSON.stringify({ kind: "lightbulb_local_session_invocation", aud: "lightbulb:local-sessions",
    ownerId: ENV.ISOL8_OWNER_ID, agentId: FRIDAY, roomId: ROOM, eventId: MESSAGE, sessionKey: sessionKeyFor(FRIDAY, ROOM),
    iat: now, exp: now + 300, jti: MESSAGE })).toString("base64url")}.runtime-only-sentinel`;
  const event = ingress();
  expect(parseIngress(event).ok).toBe(true);
  expect(parseIngress({ ...event, invocation: { grant: token } }).ok).toBe(false);
  const dispatch = vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: true }));
  const runs = turnRuns();
  const deps = { sdk: inboundSdk({ buildChannelInboundEventContext }), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs };
  await dispatchTurn(deps, event, token);
  const first = dispatch.mock.calls[0][0].ctxPayload;
  expect(first.GatewayRunToolBindings.lightbulbLocalSessions.grant).toBe(token);
  expect(first.NativeChannelId).toBe(ROOM);
  const { GatewayRunToolBindings: _private, ...publicContext } = first;
  expect(JSON.stringify(publicContext)).not.toContain(token);
  await dispatchTurn(deps, ingress());
  expect(dispatch.mock.calls[1][0].ctxPayload.GatewayRunToolBindings).toBeUndefined();
  expect(redactSensitiveText(`X-Lightbulb-Local-Session-Token: ${token}`)).not.toContain(token);

  const sdk = inboundSdk({ buildChannelInboundEventContext });
  const handler = createInboundHandler({ ...deps, sdk });
  const response = () => ({ setHeader: vi.fn(), end: vi.fn(), statusCode: 0 }) as any;
  let acked = false;
  for (const [headerValue, rawHeaders, expected] of [
    [token, ["X-Lightbulb-Local-Session-Token", token], true],
    [undefined, [], false], ["invalid", [], false], [token.repeat(20), [], false],
    [[token, token], [], false], [token, ["X-Lightbulb-Local-Session-Token", token, "x-lightbulb-local-session-token", token], false],
  ] as const) {
    const res = response();
    await handler({ headers: { "x-lightbulb-local-session-token": headerValue }, rawHeaders } as any, res);
    // Effect-once test mock replays repeated IDs; use a fresh handler below for isolation.
    expect(res.statusCode).toBe(acked ? 200 : 202);
    acked = true;
    expect(JSON.stringify(res.end.mock.calls)).not.toContain(token);
    const fresh = createInboundHandler({ ...deps, sdk });
    await fresh({ headers: { "x-lightbulb-local-session-token": headerValue }, rawHeaders } as any, response());
    await runs.settled();
    expect(Boolean(dispatch.mock.calls.at(-1)?.[0].ctxPayload.GatewayRunToolBindings)).toBe(expected);
  }
  const failingRuns = turnRuns();
  const failing = createInboundHandler({ ...deps, sdk, runs: failingRuns, channelRuntime: runtime(vi.fn(async () => { throw new Error(token); })) });
  const failed = response();
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  const warnings = vi.spyOn(console, "warn").mockImplementation(() => {});
  try {
    await failing({ headers: { "x-lightbulb-local-session-token": token }, rawHeaders: [] } as any, failed);
    expect(failed.statusCode).toBe(202);
    expect((await failingRuns.settled())[0]).toBeInstanceOf(Error);
    expect(JSON.stringify([failed.end.mock.calls, errors.mock.calls, warnings.mock.calls])).not.toContain(token);
    expect(errors).not.toHaveBeenCalled();
    expect(warnings).not.toHaveBeenCalled();
  } finally { errors.mockRestore(); warnings.mockRestore(); }
});

it.each([["DISPATCH", "dispatched"], ["IGNORE", "ignored"], ["uncertain", "gate_error"]])(
  "keeps the invocation header private across ambient %s admission and replay", async (decision, status) => {
    const now = Math.floor(Date.now() / 1000);
    const token = `eyJhbGciOiJIUzI1NiJ9.${Buffer.from(JSON.stringify({ kind: "lightbulb_local_session_invocation", aud: "lightbulb:local-sessions",
      ownerId: ENV.ISOL8_OWNER_ID, agentId: FRIDAY, roomId: ROOM, eventId: MESSAGE, sessionKey: sessionKeyFor(FRIDAY, ROOM),
      iat: now, exp: now + 300, jti: MESSAGE })).toString("base64url")}.ambient-private-sentinel`;
    const event = ingress({ eventKind: "room_event", mentions: [], message: canonicalMessage({ mentions: [] }) });
    const complete = vi.fn(async () => ({ stopReason: "stop", content: [{ type: "text", text: decision }] }));
    const sdk = inboundSdk({ buildChannelInboundEventContext,
      readJsonWebhookBodyOrReject: vi.fn(async () => ({ ok: true, value: event })) as any,
      completeWithPreparedSimpleCompletionModel: complete,
    });
    const state = handlerDeps({ sdk });
    const handler = createInboundHandler(state.deps);
    const req = { ...request(), headers: { "x-lightbulb-local-session-token": token }, rawHeaders: ["X-Lightbulb-Local-Session-Token", token] } as IncomingMessage;
    const first = responseRecorder();
    await handler(req, first);
    expect(first.statusCode).toBe(202);
    expect(JSON.parse(first.body).status).toBe("accepted");
    await expect(state.runs.settled()).resolves.toEqual([status]);
    expect(JSON.stringify(complete.mock.calls)).not.toContain(token);
    expect(complete).toHaveBeenCalledWith(expect.objectContaining({ options: expect.objectContaining({ maxTokens: 16, reasoning: "off" }) }));
    if (status === "dispatched") {
      const context = state.dispatch.mock.calls[0][0].ctxPayload;
      expect(context.GatewayRunToolBindings?.lightbulbLocalSessions.grant).toBe(token);
      const { GatewayRunToolBindings: _private, ...publicContext } = context;
      expect(JSON.stringify(publicContext)).not.toContain(token);
    } else expect(state.dispatch).not.toHaveBeenCalled();
    const replay = responseRecorder();
    await handler(req, replay);
    expect(JSON.parse(replay.body).status).toBe("replayed");
    expect(complete).toHaveBeenCalledTimes(1);
    expect(state.dispatch).toHaveBeenCalledTimes(status === "dispatched" ? 1 : 0);
    expect(first.body + replay.body).not.toContain(token);
  },
);

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
    sessions: [],
    run: RUN,
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
  const runs = turnRuns();
  return {
    deps: { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs },
    sdk,
    dispatch,
    runs,
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
  it("registers the optional adapter through the real full/tool-discovery SDK entry only", () => {
    for (const mode of ["full", "tool-discovery", "discovery", "cli-metadata"]) {
      const registerTool = vi.fn();
      const registerHttpRoute = vi.fn();
      const on = vi.fn();
      entry.register({ registrationMode: mode, registerChannel: vi.fn(), registerTool, registerHttpRoute, on, runtime: { nodes: { list: vi.fn() } } } as any);
      expect(registerTool.mock.calls.length).toBe(["full", "tool-discovery"].includes(mode) ? 1 : 0);
      if (registerTool.mock.calls.length) {
        expect(registerTool.mock.calls[0][1]).toEqual({ names: ["lightbulb_local_sessions"] });
        expect(registerTool.mock.calls[0][0]({})).toBeNull();
      }
      expect(registerHttpRoute.mock.calls.length).toBe(["full", "tool-discovery"].includes(mode) ? 1 : 0);
      if (registerHttpRoute.mock.calls.length) {
        const [params] = registerHttpRoute.mock.calls[0];
        expect(params).toMatchObject({
          path: "/plugins/lightbulb-channel/rpc",
          auth: "gateway",
          match: "exact",
          gatewayRuntimeScopeSurface: "trusted-operator",
        });
        expect(typeof params.handler).toBe("function");
      }
      expect(on.mock.calls.length).toBe(["full", "tool-discovery"].includes(mode) ? 1 : 0);
      if (on.mock.calls.length) {
        expect(on.mock.calls[0][0]).toBe("agent_end");
        expect(typeof on.mock.calls[0][1]).toBe("function");
      }
    }
  });
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
    // OpenClaw drops, with only a log line, any tool whose name the manifest does not declare.
    const registerTool = vi.fn();
    entry.register({ registrationMode: "full", registerChannel: vi.fn(), registerTool, registerHttpRoute: vi.fn(), on: vi.fn(), runtime: { nodes: { list: vi.fn() } } } as any);
    expect(registerTool.mock.calls.flatMap(([, opts]) => opts.names)).toEqual(manifest.contracts.tools);
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

  it("publishes the platform-send attempt identity before the POST", async () => {
    const order: string[] = [];
    const fetch = vi.fn(async () => {
      order.push("post");
      return new Response(
        JSON.stringify({ messageId: "canonical-1", roomId: ROOM, createdAt: CREATED_AT }),
        { status: 201, headers: { "content-type": "application/json" } },
      );
    });
    const adapter = createOutbound({ fetch, env: () => ENV });
    await adapter.sendText(
      sendContext({
        onPlatformSendDispatch: async () => void order.push("dispatch"),
        assertDirectAdapterHandoff: () => void order.push("fence"),
      }),
    );
    expect(order).toEqual(["dispatch", "fence", "post"]);
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

  it("requires a whole run contract consistent with the room", () => {
    const parsed = parseIngress(ingress());
    expect(parsed.ok && parsed.value.run).toEqual(RUN);
    const { run: _run, ...withoutRun } = ingress();
    expect(parseIngress(withoutRun).ok).toBe(false);
    expect(parseIngress(ingress({ run: { ...RUN, frameChannel: `/ws/${WORKSPACE}/rooms/${WORKSPACE}/${DELIVERY}` } })).ok).toBe(false);
    expect(parseIngress(ingress({ run: { ...RUN, frameEndpoint: "http://insecure/event" } })).ok).toBe(false);
    expect(parseIngress(ingress({ run: { runId: RUN_ID } })).ok).toBe(false);
    expect(parseIngress(ingress({ run: { ...RUN, extra: 1 } })).ok).toBe(false);
  });

  it("accepts a bounded live-session summary and defaults it to empty", () => {
    const summary = { sessionId: "33333333-3333-4333-8333-333333333333", humanUserId: "user_1", displayName: "Founder", provider: "claude", cwdName: "repo", title: "Fix login", status: "busy", latestEventSeq: 12, lastEventAt: "2026-09-14T10:00:00Z" };
    const parsed = parseIngress({ ...ingress(), sessions: [summary] });
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) throw new Error(parsed.error);
    expect(parsed.value.sessions).toEqual([summary]);

    const { sessions: _omit, ...withoutSessions } = ingress();
    const absent = parseIngress(withoutSessions);
    expect(absent.ok).toBe(true);
    if (!absent.ok) throw new Error(absent.error);
    expect(absent.value.sessions).toEqual([]);

    const odd = parseIngress({ ...ingress(), sessions: [{ ...summary, status: "sleeping", cwdName: null, title: null, lastEventAt: null }] });
    expect(odd.ok).toBe(true);
    if (!odd.ok) throw new Error(odd.error);
    expect(odd.value.sessions[0]).toMatchObject({ status: "unknown", cwdName: null, title: null, lastEventAt: null });

    expect(parseIngress({ ...ingress(), sessions: Array.from({ length: 25 }, () => summary) }).ok).toBe(false);
    expect(parseIngress({ ...ingress(), sessions: [{ ...summary, provider: "gemini" }] }).ok).toBe(false);
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

    const outcome = await dispatchTurn(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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

    const outcome = await dispatchTurn(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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

    const outcome = await dispatchTurn(
      { sdk, cfg, accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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

    const outcome = await dispatchTurn(
      { sdk, cfg, accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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

    await dispatchTurn(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs: fakeRuns() },
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
    const sessionSummary = { sessionId: "33333333-3333-4333-8333-333333333333", humanUserId: "user_1", displayName: "Founder", provider: "claude" as const, cwdName: "repo", title: "Fix login", status: "busy" as const, latestEventSeq: 12, lastEventAt: "2026-09-14T10:00:00Z" };
    await dispatchTurn(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
      { ...ingress(), sessions: [sessionSummary] },
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
    expect(contextInput.extra.ChannelStructuredContext[0].payload.sessions).toEqual([sessionSummary]);
    expect(contextInput.access).toMatchObject({
      commands: { authorized: true },
      mentions: { wasMentioned: true, explicitlyMentionedBot: true },
    });
    expect(sdk.prepareSimpleCompletionModelForAgent).not.toHaveBeenCalled();
    expect(sdk.completeWithPreparedSimpleCompletionModel).not.toHaveBeenCalled();
  });

  it("hands the agent the triggering message id and thread root in the room context", async () => {
    const sdk = inboundSdk();
    const base = ingress();
    await dispatchTurn(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs: fakeRuns() },
      { ...base, message: { ...base.message, threadRootMessageId: HISTORY_MESSAGE } },
    );
    const contextInput = vi.mocked(sdk.buildChannelInboundEventContext).mock.calls[0]![0] as any;
    expect(contextInput.extra.ChannelStructuredContext[0].payload.message).toEqual({
      id: MESSAGE,
      threadRootMessageId: HISTORY_MESSAGE,
    });
  });

  // These two exercise deps.runs.start's spec, which only createInboundHandler builds
  // (runRoomTurn, what dispatchTurn calls directly, never calls runs.start) — so, unlike
  // the test above, they go through the handler + a mocked request/response, same as the
  // "async turn ack and run controls" tests below.
  it("opens a thread under a top-level request in a channel: the run record carries it as the thread root", async () => {
    const runs = fakeRuns();
    const handler = createInboundHandler({ sdk: withBody(ingress()), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs });
    await handler(request(), responseRecorder());
    expect(runs.start).toHaveBeenCalledWith(expect.objectContaining({ threadRootMessageId: MESSAGE }), expect.any(Function));
  });

  it("keeps a threaded request in its thread: the run record carries the existing root", async () => {
    const runs = fakeRuns();
    const base = ingress();
    const handler = createInboundHandler({
      sdk: withBody({ ...base, message: { ...base.message, threadRootMessageId: HISTORY_MESSAGE } }),
      cfg: config(),
      accountId: FRIDAY,
      channelRuntime: runtime(),
      env: () => ENV,
      runs,
    });
    await handler(request(), responseRecorder());
    expect(runs.start).toHaveBeenCalledWith(expect.objectContaining({ threadRootMessageId: HISTORY_MESSAGE }), expect.any(Function));
  });

  it("keeps DM replies linear: no thread root on a top-level DM request", async () => {
    const runs = fakeRuns();
    const base = ingress();
    const handler = createInboundHandler({
      sdk: withBody({ ...base, room: { ...base.room, kind: "dm" } }),
      cfg: config(),
      accountId: FRIDAY,
      channelRuntime: runtime(),
      env: () => ENV,
      runs,
    });
    await handler(request(), responseRecorder());
    expect(runs.start).toHaveBeenCalledWith(expect.objectContaining({ threadRootMessageId: null }), expect.any(Function));
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
    await dispatchTurn(
      { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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
    await dispatchTurn(
      { sdk: inboundSdk(), cfg: config(), accountId: GAUGE, channelRuntime: runtime(reverseDispatch), env: () => ENV, runs: fakeRuns() },
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
        threadId: MESSAGE,
        requiredCapabilities: DURABLE_FINAL_CAPABILITIES,
      });
      await expect(params.delivery.deliver(final, { kind: "final" })).rejects.toThrow(
        /direct delivery is not supported/,
      );
      return { admission: { kind: "dispatch" }, dispatched: true };
    });
    await dispatchTurn(
      { sdk: inboundSdk(), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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
        threadId: MESSAGE,
        requiredCapabilities: DURABLE_FINAL_CAPABILITIES,
      });
      return { admission: { kind: "dispatch" }, dispatched: true };
    });
    await dispatchTurn(
      { sdk: inboundSdk(), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs: fakeRuns() },
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
      dispatchTurn(
        { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs: fakeRuns() },
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
      dispatchTurn(
        { sdk, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs: fakeRuns() },
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
    await expect(state.runs.settled()).resolves.toEqual(["ignored"]);
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
    expect(replay.statusCode).toBe(200);
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
    expect(JSON.parse(first.body).status).toBe("accepted");
    expect(JSON.parse(replay.body).status).toBe("replayed");
    await state.runs.settled();
    expect(state.dispatch).toHaveBeenCalledOnce();
    expect(state.sdk.createIngressEffectOnce).toHaveBeenCalledWith({
      pluginId: "lightbulb",
      namespacePrefix: `lightbulb-inbound-${FRIDAY}`,
      ...EFFECT_ONCE,
    });
  });

  it("fails the background run when native dispatch reports no dispatched turn", async () => {
    const state = handlerDeps({
      dispatch: vi.fn(async () => ({ admission: { kind: "dispatch" }, dispatched: false })),
    });
    const res = responseRecorder();
    await createInboundHandler(state.deps)(request(), res);
    expect(res.statusCode).toBe(202);
    expect((await state.runs.settled())[0]).toMatchObject({ status: 500 });
  });

  it("documents the crash window between admission and commit, which costs no turn", async () => {
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
    const crashed = handlerDeps({ sdk, dispatch });
    const first = responseRecorder();
    await createInboundHandler(crashed.deps)(request(), first);
    expect(first.statusCode).toBe(500);
    expect(crashed.runs.start).not.toHaveBeenCalled();
    crash = false;
    const retried = handlerDeps({ sdk, dispatch });
    const retry = responseRecorder();
    await createInboundHandler(retried.deps)(request(), retry);
    expect(retry.statusCode).toBe(202);
    await retried.runs.settled();
    expect(dispatch).toHaveBeenCalledOnce();
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
      runs: fakeRuns(),
      runControlHandler: vi.fn(),
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

    const inbound = registrations.filter((row) => row.source === "lightbulb-channel-gateway");
    expect(inbound.map((row) => row.path)).toEqual([
      `${ROUTE_PREFIX}${FRIDAY}`,
      `${ROUTE_PREFIX}${GAUGE}`,
    ]);
    for (const row of inbound) {
      expect(row).toMatchObject({
        auth: "gateway",
        match: "exact",
        pluginId: "lightbulb",
        source: "lightbulb-channel-gateway",
        replaceExisting: true,
        throwOnFailure: true,
      });
    }
    // Every account start re-claims the shared run-control route; `replaceExisting` keeps
    // exactly one live route, so a config reload that drops an account's routes cannot
    // leave abort/steer unreachable until the process restarts.
    const control = registrations.filter((row) => row.source === "lightbulb-channel-runs");
    expect(control).toHaveLength(2);
    for (const row of control) {
      expect(row).toMatchObject({
        path: RUNS_ROUTE_PREFIX,
        auth: "gateway",
        match: "prefix",
        pluginId: "lightbulb",
        replaceExisting: true,
        throwOnFailure: true,
        handler: deps.runControlHandler,
      });
    }
    expect(new Set(control.map((row) => row.path)).size).toBe(1);
    expect(fridayStatuses.map((status) => status.lifecycle)).toEqual(["starting", "ready"]);
    expect(gaugeStatuses.map((status) => status.lifecycle)).toEqual(["starting", "ready"]);

    fridayAbort.abort();
    gaugeAbort.abort();
    await Promise.all([friday, gauge]);
    expect(inbound.every((row) => row.unregistered)).toBe(true);
    expect(control.some((row) => row.unregistered)).toBe(false);
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

describe("async turn ack and run controls", () => {
  it("acks 202 accepted before the turn runs, 200 replayed on redelivery, 409 when not admitted", async () => {
    const dispatch = vi.fn(() => new Promise<never>(() => {})); // a turn that never ends
    const runs = fakeRuns();
    const handler = createInboundHandler({ sdk: withBody(ingress()), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs });
    const first = responseRecorder();
    await handler(request(), first);
    expect(first.statusCode).toBe(202);
    expect(JSON.parse(first.body)).toEqual({ eventId: MESSAGE, runId: RUN_ID, status: "accepted" });
    expect(runs.start).toHaveBeenCalledTimes(1);
    expect(runs.start.mock.calls[0][0]).toMatchObject({ runId: RUN_ID, roomId: ROOM, agentId: FRIDAY, frameChannel: RUN.frameChannel, frameEndpoint: RUN.frameEndpoint, eventKind: "user_request" });
    expect(dispatch).not.toHaveBeenCalled(); // the turn is the supervisor's to start
    const second = responseRecorder();
    await handler(request(), second);
    expect(second.statusCode).toBe(200);
    expect(JSON.parse(second.body)).toEqual({ eventId: MESSAGE, runId: RUN_ID, status: "replayed" });
    expect(runs.start).toHaveBeenCalledTimes(1);
    const declined = responseRecorder();
    const declining = withBody(ingress({ eventId: OTHER_MESSAGE, message: canonicalMessage({ messageId: OTHER_MESSAGE }), run: { ...RUN, runId: OTHER_RUN } }), {
      resolveStableChannelMessageIngress: vi.fn(async () => ({ ...dispatchedIngress(), ingress: { admission: "drop" } })) as any,
    });
    await createInboundHandler({ sdk: declining, cfg: config(), accountId: FRIDAY, channelRuntime: runtime(dispatch), env: () => ENV, runs })(request(), declined);
    expect(declined.statusCode).toBe(409);
    expect(runs.start).toHaveBeenCalledTimes(1);
  });

  it("rejects a body without the run contract", async () => {
    const { run: _run, ...legacy } = ingress();
    const rec = responseRecorder();
    await createInboundHandler({ sdk: withBody(legacy), cfg: config(), accountId: FRIDAY, channelRuntime: runtime(), env: () => ENV, runs: fakeRuns() })(request(), rec);
    expect(rec.statusCode).toBe(400);
  });

  it("routes abort and steer to the supervisor", async () => {
    const runs = fakeRuns();
    const at = (action: string, body: unknown = {}) => Object.assign(request(), { url: `${RUNS_ROUTE_PREFIX}${RUN_ID}/${action}`, sdkBody: body });
    const handler = createRunControlHandler({ sdk: inboundSdk({ readJsonWebhookBodyOrReject: vi.fn(async ({ req }: any) => ({ ok: true, value: req.sdkBody })) as any }), runs });
    const abort = responseRecorder();
    await handler(at("abort"), abort);
    expect(abort.statusCode).toBe(202);
    expect(runs.abort).toHaveBeenCalledWith(RUN_ID);
    const steer = responseRecorder();
    await handler(at("steer", { text: "shorter" }), steer);
    expect(steer.statusCode).toBe(202);
    expect(runs.steer).toHaveBeenCalledWith(RUN_ID, "shorter");
    const bad = responseRecorder();
    await handler(at("steer", {}), bad);
    expect(bad.statusCode).toBe(400);
    runs.abort.mockReturnValueOnce("not_found" as any);
    const gone = responseRecorder();
    await handler(at("abort"), gone);
    expect(gone.statusCode).toBe(404);
    const unknown = responseRecorder();
    await handler(Object.assign(request(), { url: "/plugins/lightbulb-channel/runs/nope" }), unknown);
    expect(unknown.statusCode).toBe(404);
  });

  it("stamps the active run id on the outbound reply body", () => {
    const runs = fakeRuns();
    const body = buildOutboundBody({ to: `lightbulb:${ROOM}`, accountId: FRIDAY, text: "hi", deliveryQueueId: "q", deliveryPartIndex: 0 } as any, runs);
    expect(body.runId).toBe(RUN_ID);
    expect(runs.runIdForSession).toHaveBeenCalledWith(sessionKeyFor(FRIDAY, ROOM));
  });
});
