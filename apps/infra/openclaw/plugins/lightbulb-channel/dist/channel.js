export const CHANNEL_ID = "lightbulb";
export const PLUGIN_ID = "lightbulb";
export const ROUTE_PREFIX = "/plugins/lightbulb-channel/inbound/";
export const MAX_BODY_BYTES = 1_048_576;
export const BODY_TIMEOUT_MS = 30_000;
export const UNKNOWN_SEND_RECONCILIATION_TIMEOUT_MS = 10_000;
export const MAX_TEXT_BYTES = 100_000;
export const MAX_EVENT_ID_BYTES = 256;
export const MAX_HISTORY = 50;
export const MAX_PARTICIPANTS = 128;
export const EFFECT_ONCE = {
    ttlMs: 8 * 24 * 60 * 60 * 1000,
    stateMaxEntries: 100_000,
    memoryMaxSize: 5_000,
};
export const DURABLE_FINAL_CAPABILITIES = {
    text: true,
    replyTo: true,
    thread: true,
    messageSendingHooks: true,
    reconcileUnknownSend: true,
};
/** Mirrors the host's canonical agent-id rule, narrowed to lowercase inputs. */
const AGENT_ID_PATTERN = "^[a-z0-9][a-z0-9_-]{0,63}$";
const AGENT_ID_RE = new RegExp(AGENT_ID_PATTERN);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UTC_RFC3339_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/;
/** The same object feeds openclaw.plugin.json (manifest validation) and buildJsonChannelConfigSchema (runtime). */
export const CHANNEL_CONFIG_JSON_SCHEMA = {
    type: "object",
    additionalProperties: false,
    properties: {
        enabled: { type: "boolean" },
        accounts: {
            type: "object",
            propertyNames: { pattern: AGENT_ID_PATTERN },
            additionalProperties: {
                type: "object",
                additionalProperties: false,
                properties: { enabled: { type: "boolean" } },
            },
        },
    },
};
function channelConfig(cfg) {
    return (cfg.channels?.[CHANNEL_ID] ??
        {});
}
export function listAccountIds(cfg) {
    return Object.keys(channelConfig(cfg).accounts ?? {})
        .filter((id) => AGENT_ID_RE.test(id))
        .sort();
}
export function resolveAccount(cfg, accountId) {
    const id = (accountId ?? "").trim();
    const channel = channelConfig(cfg);
    const account = AGENT_ID_RE.test(id) ? channel.accounts?.[id] : undefined;
    return {
        accountId: id || "default",
        configured: account !== undefined,
        enabled: channel.enabled !== false && account !== undefined && account.enabled !== false,
    };
}
export class LightbulbConfigurationError extends Error {
    name = "LightbulbConfigurationError";
}
/** ISOL8_OWNER_ID is an identifier; OPENCLAW_HOOK_TOKEN is what the backend timing-safely checks. */
export function resolveEnv(env) {
    const base = (env.ISOL8_API_URL ?? "").trim().replace(/\/$/, "");
    const ownerId = (env.ISOL8_OWNER_ID ?? "").trim();
    const token = (env.OPENCLAW_HOOK_TOKEN ?? "").trim();
    if (!base)
        throw new LightbulbConfigurationError("ISOL8_API_URL is not set on this container");
    if (!ownerId)
        throw new LightbulbConfigurationError("ISOL8_OWNER_ID is not set on this container");
    if (!token)
        throw new LightbulbConfigurationError("OPENCLAW_HOOK_TOKEN is not set on this container");
    return { base, ownerId, token };
}
export function agentMessagesPath(ownerId) {
    return `/api/v1/internal/messaging/agent-messages/${encodeURIComponent(ownerId)}`;
}
export function receiptPath(ownerId, eventId) {
    return `${agentMessagesPath(ownerId)}/receipts/${encodeURIComponent(eventId)}`;
}
/** `lightbulb:<roomUuid>` (or a bare UUID) -> the room UUID; anything else is not a target. */
export function normalizeTarget(raw) {
    const target = raw.trim().replace(/^lightbulb:/i, "");
    return UUID_RE.test(target) ? target : undefined;
}
/** Durable queue id + part index first, then the prepared id; never a minted key. */
export function resolveOutboundEventId(ctx) {
    const queueId = ctx.deliveryQueueId;
    if (queueId?.trim()) {
        const part = ctx.deliveryPartIndex ?? 0;
        if (!Number.isSafeInteger(part) || part < 0) {
            throw new Error("lightbulb: durable delivery part index is invalid");
        }
        return requireOpaqueEventId(`${queueId}:${part}`);
    }
    return requireOpaqueEventId(ctx.preparedMessageId ?? "");
}
function requireOpaqueEventId(value) {
    if (!value.trim() || Buffer.byteLength(value, "utf8") > MAX_EVENT_ID_BYTES) {
        throw new Error("lightbulb: send has no valid stable host idempotency id (queue or prepared)");
    }
    return value;
}
export function buildOutboundBody(ctx) {
    const roomId = normalizeTarget(ctx.to);
    if (!roomId)
        throw new Error("lightbulb: outbound target is not a room id");
    const agentId = (ctx.accountId ?? "").trim();
    if (!AGENT_ID_RE.test(agentId))
        throw new Error("lightbulb: outbound send has no bound account");
    return {
        eventId: resolveOutboundEventId(ctx),
        roomId,
        agentId,
        text: ctx.text,
        replyToMessageId: ctx.replyToId ?? null,
        threadRootMessageId: ctx.threadId == null ? null : String(ctx.threadId),
    };
}
function receiptResponse(value, roomId) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new Error("lightbulb: backend receipt is malformed");
    }
    const row = value;
    const keys = Object.keys(row).sort();
    if (keys.join(",") !== "createdAt,messageId,roomId") {
        throw new Error("lightbulb: backend receipt is malformed");
    }
    const messageId = typeof row.messageId === "string" ? row.messageId : "";
    const createdAt = typeof row.createdAt === "string" ? row.createdAt : "";
    const sentAt = UTC_RFC3339_RE.test(createdAt) ? Date.parse(createdAt) : NaN;
    if (!messageId.trim() || row.roomId !== roomId || !Number.isFinite(sentAt)) {
        throw new Error("lightbulb: backend receipt is malformed");
    }
    return { messageId, sentAt };
}
/** Exact `{messageId, roomId, createdAt}` -> exact 9.3 MessageReceipt. */
export function toReceipt(value, roomId) {
    const { messageId, sentAt } = receiptResponse(value, roomId);
    return {
        primaryPlatformMessageId: messageId,
        platformMessageIds: [messageId],
        parts: [{ platformMessageId: messageId, kind: "text", index: 0 }],
        sentAt,
    };
}
export function createOutbound(deps) {
    const request = async (resolved, path, init, signal) => await deps.fetch(`${resolved.base}${path}`, {
        ...init,
        headers: { ...(init.headers ?? {}), Authorization: `Bearer ${resolved.token}` },
        signal,
    });
    return {
        async sendText(ctx) {
            const resolved = resolveEnv(deps.env());
            const body = buildOutboundBody(ctx);
            const res = await request(resolved, agentMessagesPath(resolved.ownerId), { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }, ctx.signal);
            if (res.status !== 201)
                throw new Error(`lightbulb: backend rejected the message (${res.status})`);
            const receipt = toReceipt(await res.json(), body.roomId);
            return { messageId: receipt.primaryPlatformMessageId, receipt };
        },
        async reconcileUnknownSend(ctx) {
            const roomId = normalizeTarget(ctx.to);
            if (!roomId)
                return { status: "unresolved", error: "invalid room target", retryable: false };
            let eventId;
            try {
                eventId = requireOpaqueEventId(`${ctx.queueId}:0`);
            }
            catch {
                return { status: "unresolved", error: "invalid durable queue id", retryable: false };
            }
            try {
                const resolved = resolveEnv(deps.env());
                const res = await request(resolved, receiptPath(resolved.ownerId, eventId), { method: "GET" }, AbortSignal.timeout(UNKNOWN_SEND_RECONCILIATION_TIMEOUT_MS));
                if (res.status === 404)
                    return { status: "not_sent" };
                if (res.status !== 200)
                    return { status: "unresolved", retryable: true };
                const receipt = toReceipt(await res.json(), roomId);
                return { status: "sent", receipt, messageId: receipt.primaryPlatformMessageId };
            }
            catch {
                return { status: "unresolved", retryable: true };
            }
        },
    };
}
class Invalid extends Error {
}
function obj(v, path, keys) {
    if (!v || typeof v !== "object" || Array.isArray(v))
        throw new Invalid(`${path}: expected object`);
    for (const key of Object.keys(v)) {
        if (!keys.includes(key))
            throw new Invalid(`${path}.${key}: unknown field`);
    }
    return v;
}
function str(v, path) {
    if (typeof v !== "string")
        throw new Invalid(`${path}: expected string`);
    return v;
}
function nonEmpty(v, path) {
    const s = str(v, path);
    if (!s.trim())
        throw new Invalid(`${path}: expected non-empty string`);
    return s;
}
function uuid(v, path) {
    const s = str(v, path);
    if (!UUID_RE.test(s))
        throw new Invalid(`${path}: expected lowercase RFC-4122 uuid`);
    return s;
}
function nullableUuid(v, path) {
    return v === null ? null : uuid(v, path);
}
function int(v, path) {
    if (typeof v !== "number" || !Number.isInteger(v) || v < 0)
        throw new Invalid(`${path}: expected non-negative integer`);
    return v;
}
function oneOf(v, path, allowed) {
    if (typeof v !== "string" || !allowed.includes(v))
        throw new Invalid(`${path}: expected one of ${allowed.join("|")}`);
    return v;
}
function list(v, path, max) {
    if (!Array.isArray(v))
        throw new Invalid(`${path}: expected array`);
    if (v.length > max)
        throw new Invalid(`${path}: more than ${max} entries`);
    return v;
}
function memberRef(v, path) {
    const row = obj(v, path, ["kind", "id"]);
    const kind = oneOf(row.kind, `${path}.kind`, ["human", "agent"]);
    const id = nonEmpty(row.id, `${path}.id`);
    if (kind === "agent" && !AGENT_ID_RE.test(id)) {
        throw new Invalid(`${path}.id: expected lowercase agent id`);
    }
    return { kind, id };
}
function text(v, path) {
    const s = str(v, path);
    if (Buffer.byteLength(s, "utf8") > MAX_TEXT_BYTES)
        throw new Invalid(`${path}: exceeds ${MAX_TEXT_BYTES} bytes`);
    return s;
}
function rfc3339(v, path) {
    const s = str(v, path);
    if (!UTC_RFC3339_RE.test(s) || !Number.isFinite(Date.parse(s))) {
        throw new Invalid(`${path}: expected UTC RFC-3339 timestamp`);
    }
    return s;
}
const ATTACHMENT_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
};
function attachment(v, path) {
    const r = obj(v, path, ["id", "fileName", "contentType", "size"]);
    const fileName = nonEmpty(r.fileName, `${path}.fileName`);
    if (Buffer.byteLength(fileName, "utf8") > 255 || /[\\/\x00-\x1f\x7f]/u.test(fileName)) {
        throw new Invalid(`${path}.fileName: expected safe filename metadata`);
    }
    const size = int(r.size, `${path}.size`);
    if (size > 5 * 1024 * 1024)
        throw new Invalid(`${path}.size: exceeds 5242880 bytes`);
    return {
        id: uuid(r.id, `${path}.id`),
        fileName,
        contentType: oneOf(r.contentType, `${path}.contentType`, Object.keys(ATTACHMENT_EXTENSIONS)),
        size,
    };
}
function canonicalMessage(v, path) {
    const r = obj(v, path, [
        "messageId", "roomId", "messageSeq", "roomRevision", "author", "text", "mentions",
        "replyToMessageId", "threadRootMessageId", "createdAt", "runId", "attachments",
    ]);
    const authorRaw = obj(r.author, `${path}.author`, ["kind", "id", "displayName"]);
    const author = {
        kind: oneOf(authorRaw.kind, `${path}.author.kind`, ["human", "agent", "system"]),
        id: nonEmpty(authorRaw.id, `${path}.author.id`),
        displayName: nonEmpty(authorRaw.displayName, `${path}.author.displayName`),
    };
    if (author.kind === "agent" && !AGENT_ID_RE.test(author.id)) {
        throw new Invalid(`${path}.author.id: expected lowercase agent id`);
    }
    return {
        messageId: uuid(r.messageId, `${path}.messageId`),
        roomId: uuid(r.roomId, `${path}.roomId`),
        messageSeq: int(r.messageSeq, `${path}.messageSeq`),
        roomRevision: int(r.roomRevision, `${path}.roomRevision`),
        author,
        text: text(r.text, `${path}.text`),
        mentions: list(r.mentions, `${path}.mentions`, MAX_PARTICIPANTS).map((m, i) => memberRef(m, `${path}.mentions[${i}]`)),
        replyToMessageId: nullableUuid(r.replyToMessageId, `${path}.replyToMessageId`),
        threadRootMessageId: nullableUuid(r.threadRootMessageId, `${path}.threadRootMessageId`),
        createdAt: rfc3339(r.createdAt, `${path}.createdAt`),
        runId: r.runId === null ? null : nonEmpty(r.runId, `${path}.runId`),
        attachments: list(r.attachments ?? [], `${path}.attachments`, 4).map((a, i) => attachment(a, `${path}.attachments[${i}]`)),
    };
}
export function parseIngress(value) {
    try {
        const r = obj(value, "body", [
            "version", "eventId", "targetAgentId", "eventKind", "room", "sender", "message",
            "mentions", "participants", "recentHistory",
        ]);
        if (r.version !== 1)
            throw new Invalid("body.version: expected 1");
        const roomRaw = obj(r.room, "body.room", ["id", "name", "kind", "revision"]);
        const senderRaw = obj(r.sender, "body.sender", ["kind", "id", "displayName"]);
        const targetAgentId = str(r.targetAgentId, "body.targetAgentId");
        if (!AGENT_ID_RE.test(targetAgentId))
            throw new Invalid("body.targetAgentId: expected lowercase agent id");
        const room = {
            id: uuid(roomRaw.id, "body.room.id"),
            name: roomRaw.name === null ? null : str(roomRaw.name, "body.room.name"),
            kind: oneOf(roomRaw.kind, "body.room.kind", ["channel", "dm", "group-dm"]),
            revision: int(roomRaw.revision, "body.room.revision"),
        };
        const sender = {
            kind: oneOf(senderRaw.kind, "body.sender.kind", ["human", "agent", "system"]),
            id: nonEmpty(senderRaw.id, "body.sender.id"),
            displayName: nonEmpty(senderRaw.displayName, "body.sender.displayName"),
        };
        if (sender.kind === "agent" && !AGENT_ID_RE.test(sender.id)) {
            throw new Invalid("body.sender.id: expected lowercase agent id");
        }
        const message = canonicalMessage(r.message, "body.message");
        const eventId = uuid(r.eventId, "body.eventId");
        if (eventId !== message.messageId)
            throw new Invalid("body.eventId: must equal message.messageId");
        if (message.roomId !== room.id)
            throw new Invalid("body.message.roomId: must equal room.id");
        if (message.author.kind !== sender.kind ||
            message.author.id !== sender.id ||
            message.author.displayName !== sender.displayName) {
            throw new Invalid("body.sender: must equal message.author");
        }
        const mentions = list(r.mentions, "body.mentions", MAX_PARTICIPANTS).map((m, i) => memberRef(m, `body.mentions[${i}]`));
        if (JSON.stringify(mentions) !== JSON.stringify(message.mentions)) {
            throw new Invalid("body.mentions: must equal message.mentions");
        }
        const participants = list(r.participants, "body.participants", MAX_PARTICIPANTS).map((p, i) => {
            const path = `body.participants[${i}]`;
            const pr = obj(p, path, ["kind", "id", "displayName", "mentionHandle", "listenMode"]);
            const participant = memberRef({ kind: pr.kind, id: pr.id }, path);
            const mentionHandle = pr.mentionHandle === undefined ? undefined : nonEmpty(pr.mentionHandle, `${path}.mentionHandle`);
            if (mentionHandle !== undefined && !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(mentionHandle)) {
                throw new Invalid(`${path}.mentionHandle: expected lowercase mention handle`);
            }
            return {
                ...participant,
                displayName: nonEmpty(pr.displayName, `${path}.displayName`),
                ...(mentionHandle !== undefined ? { mentionHandle } : {}),
                ...(pr.listenMode !== undefined
                    ? { listenMode: oneOf(pr.listenMode, `${path}.listenMode`, ["active", "mentions", "observe"]) }
                    : {}),
            };
        });
        if (!participants.some((participant) => participant.kind === "agent" && participant.id === targetAgentId)) {
            throw new Invalid("body.participants: target agent is absent");
        }
        const recentHistory = list(r.recentHistory, "body.recentHistory", MAX_HISTORY).map((entry, i) => canonicalMessage(entry, `body.recentHistory[${i}]`));
        let previousSeq = -1;
        for (const [index, history] of recentHistory.entries()) {
            if (history.roomId !== room.id) {
                throw new Invalid(`body.recentHistory[${index}].roomId: must equal room.id`);
            }
            if (history.messageSeq >= message.messageSeq || history.messageSeq <= previousSeq) {
                throw new Invalid("body.recentHistory: expected prior messages in oldest-to-newest order");
            }
            previousSeq = history.messageSeq;
        }
        return {
            ok: true,
            value: {
                version: 1,
                eventId,
                targetAgentId,
                eventKind: oneOf(r.eventKind, "body.eventKind", ["user_request", "room_event"]),
                room,
                sender,
                message,
                mentions,
                participants,
                recentHistory,
            },
        };
    }
    catch (error) {
        if (error instanceof Invalid)
            return { ok: false, error: error.message };
        throw error;
    }
}
/** Exact 9.3 HistoryEntry rows; canonical DTOs never reach the SDK directly. */
export function toHistoryEntries(rows) {
    return rows.map((row) => ({
        sender: row.author.displayName,
        body: row.text,
        timestamp: Date.parse(row.createdAt),
        messageId: row.messageId,
    }));
}
export function sessionKeyFor(agentId, roomId) {
    return `agent:${agentId}:${CHANNEL_ID}:channel:${roomId}`;
}
/** Status-bearing rejection raised inside the guarded effect; maps to the HTTP reply. */
export class IngressRejected extends Error {
    status;
    details;
    constructor(status, message, details) {
        super(message);
        this.status = status;
        this.details = details;
    }
}
function json(res, status, body) {
    res.statusCode = status;
    res.setHeader("content-type", "application/json; charset=utf-8");
    res.setHeader("cache-control", "no-store");
    res.end(JSON.stringify(body));
    return true;
}
export async function dispatchRoomEvent(deps, event) {
    const { sdk, cfg, accountId, channelRuntime } = deps;
    const { room, sender, message, eventKind } = event;
    const { route, buildEnvelope } = sdk.resolveChannelInboundRouteEnvelope({
        cfg,
        channel: CHANNEL_ID,
        accountId,
        peer: { kind: "channel", id: room.id },
    });
    const expectedSessionKey = sessionKeyFor(accountId, room.id);
    if (route.agentId !== accountId || route.sessionKey !== expectedSessionKey) {
        throw new IngressRejected(500, "lightbulb: route does not resolve to the account's room session", {
            agentId: route.agentId,
            sessionKey: route.sessionKey,
        });
    }
    const wasMentioned = event.mentions.some((m) => m.kind === "agent" && m.id === accountId);
    // Only an authenticated human asking for something may run commands.
    const commandAuthorized = eventKind === "user_request" && sender.kind === "human";
    const ingress = await sdk.resolveStableChannelMessageIngress({
        channelId: CHANNEL_ID,
        accountId,
        cfg,
        identity: { key: "sender", entryIdPrefix: "lightbulb-entry" },
        subject: { stableId: sender.id },
        conversation: { kind: "channel", id: room.id, ...(room.name ? { title: room.name } : {}) },
        contextBinding: {
            agentId: route.agentId,
            sessionKey: route.sessionKey,
            messageId: event.eventId,
            inboundEventKind: eventKind,
        },
        // The gateway token plus Lightbulb's membership check is the boundary; the
        // SDK otherwise defaults every group ingress to disabled.
        groupPolicy: "open",
        mentionFacts: { canDetectMention: true, wasMentioned, hasAnyMention: event.mentions.length > 0 },
        ...(commandAuthorized ? {} : { command: false }),
    });
    if (ingress.ingress.admission !== "dispatch") {
        throw new IngressRejected(409, "lightbulb: channel ingress declined the event", {
            admission: ingress.ingress.admission,
        });
    }
    const timestamp = Date.parse(message.createdAt);
    const target = `${CHANNEL_ID}:${room.id}`;
    const ctxPayload = sdk.buildChannelInboundEventContext({
        channel: CHANNEL_ID,
        accountId: route.accountId ?? accountId,
        messageId: event.eventId,
        messageIdFull: event.eventId,
        timestamp,
        from: `${CHANNEL_ID}:${sender.id}`,
        sender: { id: sender.id, name: sender.displayName, isBot: sender.kind !== "human", isSelf: false },
        conversation: {
            kind: "channel",
            id: room.id,
            ...(room.name ? { label: room.name } : {}),
            routePeer: { kind: "channel", id: room.id },
        },
        route: {
            agentId: route.agentId,
            dmScope: route.dmScope,
            accountId: route.accountId,
            routeSessionKey: route.sessionKey,
            dispatchSessionKey: route.sessionKey,
        },
        reply: {
            to: target,
            originatingTo: target,
            ...(message.replyToMessageId ? { replyToId: message.replyToMessageId } : {}),
            ...(message.threadRootMessageId ? { messageThreadId: message.threadRootMessageId } : {}),
        },
        message: {
            inboundEventKind: eventKind,
            body: buildEnvelope({ channel: "Lightbulb", from: sender.displayName, timestamp, body: message.text }),
            bodyForAgent: message.text,
            rawBody: message.text,
            commandBody: message.text,
            inboundHistory: toHistoryEntries(event.recentHistory),
        },
        media: message.attachments.map((item) => ({
            path: `/home/node/.openclaw/media/messaging/attachments/${room.id}/${item.id}${ATTACHMENT_EXTENSIONS[item.contentType]}`,
            contentType: item.contentType,
            kind: "image",
            fileName: item.fileName,
            sizeBytes: item.size,
            messageId: message.messageId,
        })),
        access: {
            commands: { authorized: commandAuthorized },
            mentions: {
                canDetectMention: true,
                wasMentioned,
                hasAnyMention: event.mentions.length > 0,
                explicitlyMentionedBot: wasMentioned,
                mentionedUserIds: event.mentions.filter((m) => m.kind === "human").map((m) => m.id),
            },
        },
        channelIngress: ingress,
        extra: {
            ChannelStructuredContext: [
                {
                    label: "Lightbulb room",
                    type: "lightbulb_room",
                    payload: {
                        room,
                        sender,
                        mentions: event.mentions,
                        participants: event.participants,
                        eventKind,
                    },
                },
            ],
            ...(commandAuthorized ? {} : { CommandInterpretationSuppressed: true }),
        },
    });
    const botLoopProtection = sender.kind === "agent"
        ? {
            scopeId: `${CHANNEL_ID}:${resolveEnv(deps.env()).ownerId}`,
            conversationId: room.id,
            senderId: sender.id,
            receiverId: accountId,
            eventId: event.eventId,
            defaultsConfig: cfg.channels?.defaults?.botLoopProtection,
            defaultEnabled: true,
        }
        : undefined;
    const dispatch = await channelRuntime.inbound.dispatch({
        cfg,
        channel: CHANNEL_ID,
        accountId,
        route: { agentId: route.agentId, dmScope: route.dmScope, sessionKey: route.sessionKey },
        ctxPayload,
        messageId: event.eventId,
        ...(botLoopProtection ? { botLoopProtection } : {}),
        replyPipeline: {},
        delivery: {
            // Addressed requests deliver their final through the durable message
            // adapter (stable queue id); ambient room events never auto-post.
            preparePayload: (payload, info) => eventKind === "user_request" && info.kind === "final" ? payload : null,
            durable: eventKind === "user_request"
                ? {
                    to: target,
                    replyToId: message.replyToMessageId,
                    threadId: message.threadRootMessageId ?? undefined,
                    requiredCapabilities: DURABLE_FINAL_CAPABILITIES,
                }
                : false,
            deliver: async () => {
                throw new Error("lightbulb: direct delivery is not supported; finals must use the durable adapter");
            },
        },
    });
    if (dispatch.admission.kind !== "dispatch") {
        throw new IngressRejected(409, "lightbulb: channel declined the turn", {
            admission: dispatch.admission.kind,
            reason: dispatch.admission.reason,
        });
    }
    if (!dispatch.dispatched) {
        throw new IngressRejected(500, "lightbulb: channel accepted the turn without dispatching it");
    }
}
export function createInboundHandler(deps) {
    const { sdk, accountId } = deps;
    const effectOnce = sdk.createIngressEffectOnce({
        pluginId: PLUGIN_ID,
        namespacePrefix: `lightbulb-inbound-${accountId}`,
        ...EFFECT_ONCE,
    });
    return async (req, res) => {
        if (!sdk.applyBasicWebhookRequestGuards({ req, res, allowMethods: ["POST"], requireJsonContentType: true })) {
            return true;
        }
        const body = await sdk.readJsonWebhookBodyOrReject({
            req,
            res,
            maxBytes: MAX_BODY_BYTES,
            timeoutMs: BODY_TIMEOUT_MS,
            invalidJsonMessage: "invalid JSON body",
        });
        if (!body.ok)
            return true;
        const parsed = parseIngress(body.value);
        if (!parsed.ok)
            return json(res, 400, { error: "invalid_ingress", message: parsed.error });
        const event = parsed.value;
        if (event.targetAgentId !== accountId) {
            return json(res, 400, { error: "invalid_ingress", message: "targetAgentId does not match this route" });
        }
        if (event.sender.kind === "agent" && event.sender.id === accountId) {
            return json(res, 409, { error: "not_admitted", message: "an agent never receives its own message" });
        }
        try {
            const outcome = await effectOnce.runOnce({
                eventId: event.eventId,
                effect: "dispatch",
                run: () => dispatchRoomEvent(deps, event),
            });
            return json(res, 202, { eventId: event.eventId, status: outcome.kind === "replayed" ? "replayed" : "dispatched" });
        }
        catch (error) {
            if (error instanceof IngressRejected) {
                return json(res, error.status, {
                    error: error.status === 409 ? "not_admitted" : "dispatch_failed",
                    message: error.message,
                    ...error.details,
                });
            }
            return json(res, 500, { error: "dispatch_failed", message: "lightbulb: inbound dispatch failed" });
        }
    };
}
/** Own one exact gateway-auth route for one configured agent account. */
export async function startGatewayAccount(ctx, deps) {
    const { account } = ctx;
    if (!AGENT_ID_RE.test(ctx.accountId) ||
        account.accountId !== ctx.accountId ||
        !account.configured ||
        !account.enabled) {
        throw new Error("lightbulb: channel account is not configured and enabled");
    }
    ctx.setStatus({
        accountId: account.accountId,
        running: true,
        connected: false,
        lifecycle: "starting",
        configured: true,
        enabled: true,
    });
    let unregister;
    try {
        if (!ctx.channelRuntime) {
            throw new Error("lightbulb: channel runtime is unavailable");
        }
        const handler = createInboundHandler({
            sdk: deps.sdk,
            cfg: ctx.cfg,
            accountId: account.accountId,
            channelRuntime: ctx.channelRuntime,
            env: deps.env,
        });
        unregister = deps.registerPluginHttpRoute({
            path: `${ROUTE_PREFIX}${account.accountId}`,
            auth: "gateway",
            match: "exact",
            pluginId: PLUGIN_ID,
            source: "lightbulb-channel-gateway",
            accountId: account.accountId,
            replaceExisting: true,
            throwOnFailure: true,
            handler,
        });
        ctx.setStatus(deps.channelReadyPatch({ accountId: account.accountId }));
        await deps.waitUntilAbort(ctx.abortSignal);
    }
    finally {
        unregister?.();
        ctx.setStatus(deps.channelStoppedPatch({ accountId: account.accountId }));
    }
}
