/**
 * @lightbulb/shared — framework-neutral core for Lightbulb clients.
 *
 * Contents: adapter contract, API client core, the AppSync live-event client,
 * and platform-parameterized data hooks.
 */
export type { PlatformAdapter } from "./adapter";

export { useRuntimeRpc, useRuntimeRpcMutation } from "./hooks/useRuntimeRpc";
export type { RuntimeRpcSource, RpcResult } from "./hooks/useRuntimeRpc";
export {
  agentDisplayName,
  getAgentModelString,
} from "./hooks/agents";
export { useAgents } from "./hooks/useAgents";
export type {
  AgentsAnalyticsEvent,
  AgentsRuntimeSource,
  UseAgentsOptions,
} from "./hooks/useAgents";
export type {
  Agent,
  AgentModelRef,
  AgentsListResponse,
} from "./hooks/agents";
export { useChatGPTOAuth } from "./hooks/useChatGPTOAuth";
export type { ChatGPTOAuthOptions, OAuthState } from "./hooks/useChatGPTOAuth";
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
  PROVISION_STUCK_MS,
  formatProvisionElapsed,
} from "./hooks/provisioningCopy";
export { useOnboardingStatus } from "./hooks/useOnboardingStatus";
export type {
  UseOnboardingStatusOptions,
  UseOnboardingStatusResult,
  VisibilityAdapter,
} from "./hooks/useOnboardingStatus";

export { ApiError } from "./api/error";
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

export { accumulateStreamText } from "./live/streamText";
export {
  AppSyncEventsClient,
  APPSYNC_EVENT_WS_PROTOCOL,
  base64Url,
  buildSubprotocols,
  reconnectDelayMs,
} from "./live/appsyncEvents";
export type {
  AppSyncEventsClientOptions,
  LiveErrorKind,
  LiveStatus,
} from "./live/appsyncEvents";
export { useLiveEvents } from "./live/useLiveEvents";
export type { UseLiveEventsOptions, UseLiveEventsResult } from "./live/useLiveEvents";
export { parseLiveEvent } from "./live/types";
export type {
  ChatIncomingMessage,
  LiveEvent,
  LiveEventOf,
  LiveEventType,
  PaperclipEvent,
  PaperclipEventName,
  Question,
  QuestionAnswers,
  QuestionOption,
  QuestionRecord,
  QuestionResolvedEvent,
  QuestionSecretStoreBinding,
  QuestionStatus,
  RoomRunFrame,
  ToolResultBlock,
} from "./live/types";
export { createMessagingClient } from "./messaging/client";
export type { MessagingClient } from "./messaging/client";
export type {
  AbortRunResponse,
  ApiErrorCode,
  CanonicalRoomMessage,
  CreateMessageRequest,
  CreateRoomRequest,
  ListenMode,
  LiveConfig,
  MemberKind,
  MemberRef,
  MemberRole,
  MessagePage,
  MessagingApiError,
  PatchRoomRequest,
  ReadCursorRequest,
  ReadCursorResponse,
  RoomDetail,
  RoomKind,
  RoomListResponse,
  RoomMember,
  RoomSummary,
  RunStatusResponse,
  SteerRunRequest,
  SteerRunResponse,
  TypingRequest,
  UpsertRoomMemberRequest,
} from "./messaging/types";
export {
  createRuntimeClient,
  isRetryableRuntimeError,
  RuntimeRpcError,
  runtimeRetryDelayMs,
} from "./runtime/client";
export type { RuntimeClient } from "./runtime/client";
export type {
  RuntimeApiError,
  RuntimeApprovalDecision,
  RuntimeApprovalResolveRequest,
  RuntimeError,
  RuntimeOperation,
  RuntimeQuestionResolveRequest,
  RuntimeRpcOutcome,
  RuntimeRpcRequest,
} from "./runtime/types";

export {
  extractResultText,
  isAssistantHeartbeatAck,
  isAssistantSilentReply,
  isEmptyUserTextOnlyMessage,
  isSyntheticExecWrapper,
  isSyntheticTranscriptRepairToolResult,
  isTextBlock,
  isThinkingBlock,
  isToolResultBlock,
  isToolResultRole,
  isToolUseBlock,
  persistedEntryId,
  resolveMessageBlocks,
  resolveToolArgs,
  resolveToolUseId,
  shouldHideHistoryMessage,
} from "./chat/history";
export type { ContentBlock, HistoryMessage } from "./chat/history";
export { SESSION_ID_RE, buildChatSessionKey, sessionIdFromKey } from "./chat/sessionKey";
