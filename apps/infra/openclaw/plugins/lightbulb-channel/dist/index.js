import { randomUUID } from "node:crypto";
import { buildJsonChannelConfigSchema } from "openclaw/plugin-sdk/channel-config-schema";
import { localSessionTool } from "./local-sessions.js";
import { 
// `abortEmbeddedAgentRun` is only reachable under its public alias.
abortAgentHarnessRun as abortEmbeddedAgentRun, onAgentEvent, queueAgentHarnessMessage, resolveActiveEmbeddedRunSessionId, } from "openclaw/plugin-sdk/agent-harness-runtime";
import { getSessionEntry } from "openclaw/plugin-sdk/session-store-runtime";
import { createFramePublisher, ecsCredentialsProvider } from "./appsync.js";
import { createRunCallbacks, createRunSupervisor, postToBackend } from "./runs.js";
import { createUsageHook } from "./usage.js";
import { buildChannelOutboundSessionRoute, createChatChannelPlugin, defineChannelPluginEntry, } from "openclaw/plugin-sdk/channel-core";
import { buildChannelInboundEventContext, resolveChannelInboundRouteEnvelope } from "openclaw/plugin-sdk/channel-inbound";
import { resolveStableChannelMessageIngress } from "openclaw/plugin-sdk/channel-ingress-runtime";
import { defineChannelMessageAdapter, waitUntilAbort, } from "openclaw/plugin-sdk/channel-outbound";
import { channelReadyPatch, channelStoppedPatch } from "openclaw/plugin-sdk/gateway-runtime";
import { dispatchGatewayMethod } from "openclaw/plugin-sdk/gateway-method-runtime";
import { createIngressEffectOnce } from "openclaw/plugin-sdk/ingress-effect-once";
// @ts-expect-error OpenClaw 2026.9.3 exports this runtime JS subpath but omits its declaration file.
import { completeWithPreparedSimpleCompletionModel, prepareSimpleCompletionModelForAgent } from "openclaw/plugin-sdk/simple-completion-runtime";
import { createComputedAccountStatusAdapter, createDefaultChannelRuntimeState, } from "openclaw/plugin-sdk/status-helpers";
import { applyBasicWebhookRequestGuards, readJsonWebhookBodyOrReject, registerPluginHttpRoute, } from "openclaw/plugin-sdk/webhook-ingress";
import { CHANNEL_CONFIG_JSON_SCHEMA, CHANNEL_ID, DURABLE_FINAL_CAPABILITIES, PLUGIN_ID, createOutbound, createRunControlHandler, listAccountIds, normalizeTarget, resolveAccount, startGatewayAccount, } from "./channel.js";
import { RPC_ROUTE, createRpcHandler } from "./rpc.js";
export const lightbulbChannelConfigSchema = buildJsonChannelConfigSchema(CHANNEL_CONFIG_JSON_SCHEMA, { cacheKey: "lightbulb-channel-config:v1" });
const fetchLike = (url, init) => fetch(url, init);
const runs = createRunSupervisor({
    sdk: {
        onAgentEvent,
        resolveActiveEmbeddedRunSessionId,
        abortEmbeddedAgentRun,
        queueAgentHarnessMessage,
        // readConsistency "latest": usage is read at run end, where a validity-token-cached
        // snapshot would still carry the previous turn's totals.
        getSessionEntry: ({ agentId, sessionKey }) => getSessionEntry({ agentId, sessionKey, env: process.env, readConsistency: "latest" }),
    },
    callbacks: createRunCallbacks({ fetch: fetchLike, env: () => process.env, log: (m) => console.warn(m) }),
    frames: createFramePublisher({
        fetch: fetchLike,
        region: process.env.AWS_REGION ?? "us-east-1",
        credentials: ecsCredentialsProvider(() => process.env, fetchLike),
        log: (m) => console.warn(m),
    }),
    uuid: () => randomUUID(),
    now: () => new Date(),
});
const outbound = createOutbound({
    fetch: fetchLike,
    env: () => process.env,
    runs,
});
export const lightbulbMessageAdapter = defineChannelMessageAdapter({
    id: CHANNEL_ID,
    durableFinal: {
        capabilities: DURABLE_FINAL_CAPABILITIES,
        automaticUnknownSendReconciliation: true,
        reconcileUnknownSendKinds: { text: true },
        reconcileUnknownSend: outbound.reconcileUnknownSend,
    },
    send: { text: outbound.sendText },
});
const status = createComputedAccountStatusAdapter({
    defaultRuntime: createDefaultChannelRuntimeState("default"),
    buildChannelSummary: ({ snapshot }) => ({
        configured: snapshot.configured ?? false,
        running: snapshot.running ?? false,
    }),
    resolveAccountSnapshot: ({ account }) => ({
        accountId: account.accountId,
        configured: account.configured,
        enabled: account.enabled,
    }),
});
export const lightbulbChannelPlugin = createChatChannelPlugin({
    base: {
        id: CHANNEL_ID,
        meta: {
            id: CHANNEL_ID,
            label: "Lightbulb",
            selectionLabel: "Lightbulb rooms",
            docsPath: "/channels/lightbulb",
            docsLabel: "lightbulb",
            blurb: "Participate in canonical Lightbulb rooms as an attributed AI teammate.",
        },
        capabilities: { chatTypes: ["group", "channel"], reply: true, threads: true },
        reload: { configPrefixes: ["channels.lightbulb"], accountScopedRestart: true },
        configSchema: lightbulbChannelConfigSchema,
        config: {
            listAccountIds,
            resolveAccount,
            defaultAccountId: (cfg) => listAccountIds(cfg)[0] ?? "default",
            isConfigured: (account) => account.configured,
            isEnabled: (account) => account.enabled,
        },
        messaging: {
            targetPrefixes: [CHANNEL_ID],
            normalizeTarget,
            inferTargetChatType: () => "channel",
            targetResolver: {
                looksLikeId: (raw) => normalizeTarget(raw) !== undefined,
                hint: "lightbulb:<room-uuid>",
            },
            resolveOutboundSessionRoute: ({ cfg, agentId, accountId, target }) => {
                const roomId = normalizeTarget(target);
                const account = resolveAccount(cfg, accountId);
                if (!roomId || !account.configured || !account.enabled)
                    return null;
                if (agentId !== account.accountId) {
                    throw new Error("lightbulb: outbound agent does not match the selected account");
                }
                return buildChannelOutboundSessionRoute({
                    cfg,
                    agentId,
                    channel: CHANNEL_ID,
                    accountId: account.accountId,
                    recipientSessionExact: true,
                    peer: { kind: "channel", id: roomId },
                    chatType: "channel",
                    from: `${CHANNEL_ID}:${account.accountId}`,
                    to: `${CHANNEL_ID}:${roomId}`,
                });
            },
        },
        status,
        gateway: {
            startAccount: async (ctx) => await startGatewayAccount(ctx, {
                sdk: {
                    applyBasicWebhookRequestGuards,
                    readJsonWebhookBodyOrReject,
                    resolveChannelInboundRouteEnvelope,
                    resolveStableChannelMessageIngress,
                    buildChannelInboundEventContext,
                    createIngressEffectOnce,
                    prepareSimpleCompletionModelForAgent,
                    completeWithPreparedSimpleCompletionModel,
                },
                env: () => process.env,
                registerPluginHttpRoute,
                waitUntilAbort,
                channelReadyPatch,
                channelStoppedPatch,
                runs,
                runControlHandler: createRunControlHandler({
                    sdk: { applyBasicWebhookRequestGuards, readJsonWebhookBodyOrReject },
                    runs,
                }),
            }),
        },
        message: lightbulbMessageAdapter,
    },
});
export default defineChannelPluginEntry({
    id: PLUGIN_ID,
    name: "Lightbulb Channel",
    description: "Native OpenClaw channel for canonical Lightbulb multiplayer rooms.",
    plugin: lightbulbChannelPlugin,
    configSchema: lightbulbChannelConfigSchema,
    registerFull(api) {
        api.registerTool((ctx) => localSessionTool(ctx, {
            env: () => process.env,
            fetch: (url, init) => fetch(url, init),
            nodes: () => api.runtime.nodes.list({ connected: true }),
        }), { names: ["lightbulb_local_sessions"] });
        // Registered here, not through registerPluginHttpRoute: only the plugin-API path reads the
        // manifest's gatewayMethodDispatch contract and grants dispatchGatewayMethod to the route.
        api.registerHttpRoute({
            path: RPC_ROUTE,
            auth: "gateway",
            match: "exact",
            gatewayRuntimeScopeSurface: "trusted-operator",
            handler: createRpcHandler({ dispatch: dispatchGatewayMethod, readBody: readJsonWebhookBodyOrReject }),
        });
        api.on("agent_end", createUsageHook({
            post: async (path, body) => {
                const delivered = await postToBackend({ fetch: fetchLike, env: () => process.env }, (base, owner) => `${base}${path}/${owner}`, body);
                if (!delivered)
                    console.warn(`lightbulb: usage callback undelivered`);
            },
            getSessionEntry: ({ agentId, sessionKey }) => getSessionEntry({ agentId, sessionKey, env: process.env, readConsistency: "latest" }),
            log: (m) => console.warn(m),
        }));
    },
});
