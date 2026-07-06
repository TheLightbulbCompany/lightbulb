/**
 * @isol8/shared — framework-neutral core for Isol8 clients.
 *
 * Phase 1 state: adapter contract, API client core, and the gateway
 * WebSocket protocol machine. Data hooks (useAgentChat & co.) move here
 * next — see docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md.
 */
export type { PlatformAdapter } from "./adapter";

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
