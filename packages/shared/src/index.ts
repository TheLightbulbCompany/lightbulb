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
export { useChatGPTOAuth } from "./hooks/useChatGPTOAuth";
export type { OAuthState } from "./hooks/useChatGPTOAuth";
export { classifyContainerStatus, parseBlockedPayload } from "./hooks/containerStatus";
export type {
  BlockedPayload,
  ClassifiedStatus,
  ContainerStatusBody,
} from "./hooks/containerStatus";
export { deriveUIPhase } from "./hooks/statusContract";
export type {
  BootstrapState,
  DerivedUIPhase,
  FunnelState,
  Lifecycle,
  RuntimePhase,
  StatusContract,
  UIPhase,
} from "./hooks/statusContract";
export {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS,
  PROVISION_TIMEOUT_MS,
  formatProvisionElapsed,
} from "./hooks/provisioningCopy";
export { useOnboardingStatus } from "./hooks/useOnboardingStatus";
export type {
  UseOnboardingStatusOptions,
  UseOnboardingStatusResult,
  VisibilityAdapter,
} from "./hooks/useOnboardingStatus";

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
export { accumulateStreamText } from "./gateway/streamText";
export type {
  GatewayConnectionOptions,
  GatewayConnectionValue,
} from "./gateway/useGatewayConnection";
export type {
  ChatIncomingMessage,
  GatewayEvent,
  ToolResultBlock,
} from "./gateway/types";
