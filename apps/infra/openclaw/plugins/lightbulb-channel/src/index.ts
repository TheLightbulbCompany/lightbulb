import { buildJsonChannelConfigSchema } from "openclaw/plugin-sdk/channel-config-schema";
import { localSessionTool } from "./local-sessions.js";
import {
  buildChannelOutboundSessionRoute,
  createChatChannelPlugin,
  defineChannelPluginEntry,
} from "openclaw/plugin-sdk/channel-core";
import { buildChannelInboundEventContext, resolveChannelInboundRouteEnvelope } from "openclaw/plugin-sdk/channel-inbound";
import { resolveStableChannelMessageIngress } from "openclaw/plugin-sdk/channel-ingress-runtime";
import {
  defineChannelMessageAdapter,
  waitUntilAbort,
} from "openclaw/plugin-sdk/channel-outbound";
import { channelReadyPatch, channelStoppedPatch } from "openclaw/plugin-sdk/gateway-runtime";
import { createIngressEffectOnce } from "openclaw/plugin-sdk/ingress-effect-once";
// @ts-expect-error OpenClaw 2026.9.3 exports this runtime JS subpath but omits its declaration file.
import { completeWithPreparedSimpleCompletionModel, prepareSimpleCompletionModelForAgent } from "openclaw/plugin-sdk/simple-completion-runtime";
import {
  createComputedAccountStatusAdapter,
  createDefaultChannelRuntimeState,
} from "openclaw/plugin-sdk/status-helpers";
import {
  applyBasicWebhookRequestGuards,
  readJsonWebhookBodyOrReject,
  registerPluginHttpRoute,
} from "openclaw/plugin-sdk/webhook-ingress";
import {
  CHANNEL_CONFIG_JSON_SCHEMA,
  CHANNEL_ID,
  DURABLE_FINAL_CAPABILITIES,
  PLUGIN_ID,
  createOutbound,
  listAccountIds,
  normalizeTarget,
  resolveAccount,
  startGatewayAccount,
  type LightbulbAccount,
} from "./channel.js";

export const lightbulbChannelConfigSchema = buildJsonChannelConfigSchema(
  CHANNEL_CONFIG_JSON_SCHEMA,
  { cacheKey: "lightbulb-channel-config:v1" },
);

const outbound = createOutbound({
  fetch: async (url, init) => await fetch(url, init),
  env: () => process.env,
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

const status = createComputedAccountStatusAdapter<LightbulbAccount>({
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
        if (!roomId || !account.configured || !account.enabled) return null;
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
      startAccount: async (ctx) =>
        await startGatewayAccount(ctx, {
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
  },
});
