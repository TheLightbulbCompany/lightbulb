/**
 * @isol8/shared — framework-neutral core for Isol8 clients.
 *
 * Contents: adapter contract, API client core, the gateway WebSocket
 * protocol machine, and the platform-parameterized data hooks. useAgentChat
 * deliberately stays web-side for now (2k lines, deeply coupled to the web
 * chat-history/timeline model) — mobile builds its lean chat on the gateway
 * primitives until parity demands the full extraction. See
 * docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md.
 */
export type { PlatformAdapter } from "./adapter";

export { useGatewayRpc, useGatewayRpcMutation } from "./hooks/useGatewayRpc";
export type { GatewayRpcSource, RpcResult } from "./hooks/useGatewayRpc";
export {
  agentDisplayName,
  getAgentModelString,
} from "./hooks/agents";
export type {
  Agent,
  AgentModelRef,
  AgentsListResponse,
} from "./hooks/agents";

export { ApiError } from "./api/error";
export { deriveWebSocketUrl } from "./api/url";
export {
  REQUEST_ID_HEADER,
  createApiClient,
  createAuthenticatedFetch,
  fetchJsonWithStatus,
} from "./api/client";
export type {
  ApiCallOptions,
  ApiClientConfig,
  ApiMethods,
  UploadResponse,
  UploadedFile,
} from "./api/client";

export { useGatewayConnection } from "./gateway/useGatewayConnection";
export type {
  GatewayConnectionOptions,
  GatewayConnectionValue,
} from "./gateway/useGatewayConnection";
export type {
  ChatIncomingMessage,
  GatewayEvent,
  ToolResultBlock,
} from "./gateway/types";
