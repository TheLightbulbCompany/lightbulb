/**
 * @lightbulb/shared — framework-neutral core for Lightbulb clients.
 *
 * Contents: adapter contract, API client core, the AppSync live-event hook,
 * and platform-parameterized data hooks.
 *
 * This barrel is the package's public surface: every line here is imported by
 * `apps/frontend`, `apps/mobile` or both — with two deliberate exceptions that
 * have no consumer yet and must NOT be pruned: everything originating in
 * `messaging/{useRooms,timeline,mentions}.ts`, held for the mobile port onto the
 * shared room machine, and the approvals/questions rail (`Question*`, the
 * runtime approval/question resolve requests), kept pending the owner's call on
 * a renderer. Internals stay internal — import them relatively from inside the
 * package instead of re-exporting them here.
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
  Agent,
  AgentModelRef,
  AgentsListResponse,
} from "./hooks/agents";
export { useChatGPTOAuth } from "./hooks/useChatGPTOAuth";
export type { ChatGPTOAuthOptions, OAuthState } from "./hooks/useChatGPTOAuth";
export type {
  ClassifiedStatus,
  ContainerStatusBody,
} from "./hooks/containerStatus";
export { deriveUIPhase } from "./hooks/statusContract";
export type { StatusContract, UIPhase } from "./hooks/statusContract";
export {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS,
  PROVISION_TIMEOUT_MS,
  PROVISION_STUCK_MS,
  formatProvisionElapsed,
} from "./hooks/provisioningCopy";
export { useOnboardingStatus } from "./hooks/useOnboardingStatus";
export type { VisibilityAdapter } from "./hooks/useOnboardingStatus";

export { ApiError } from "./api/error";
export { createApiClient, fetchJsonWithStatus } from "./api/client";
export type { ApiCallOptions, ApiMethods } from "./api/client";

export { accumulateStreamText } from "./live/streamText";
export { useLiveEvents } from "./live/useLiveEvents";
export type {
  ChatIncomingMessage,
  LiveEvent,
  LiveEventOf,
  Question,
  QuestionAnswers,
  QuestionOption,
  QuestionRecord,
  QuestionResolvedEvent,
  QuestionSecretStoreBinding,
  QuestionStatus,
} from "./live/types";
export { createMessagingClient } from "./messaging/client";
export {
  GROUP_WINDOW_MS,
  buildTimeline,
  dateDividerLabel,
  frameText,
  lastSequence,
  mergeCanonicalMessages,
  roomDisplayName,
  runView,
} from "./messaging/timeline";
export type {
  LiveRoomRun,
  RoomRunView,
  RunToolCall,
  TimelineItem,
} from "./messaging/timeline";
export {
  MENTION_SCHEME,
  activeTrigger,
  applyCompletion,
  linkifyMentions,
  parseMentionHref,
  resolveTypedMentions,
} from "./messaging/mentions";
export type { CompletionTrigger, MentionTarget, TriggerKind } from "./messaging/mentions";
export { useRooms } from "./messaging/useRooms";
export type {
  OptimisticRoomMessage,
  RoomsLiveSource,
  RoomsPlatform,
  SendState,
  TypingMember,
  UseRoomsOptions,
  UseRoomsResult,
} from "./messaging/useRooms";
export type {
  LocalSession,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
  LocalSessionEvent,
  LocalSessionEventPage,
  RoomResources,
  CanonicalRoomMessage,
  CreateMessageRequest,
  UpdateMessageRequest,
  ImageAttachment,
  ListenMode,
  LiveConfig,
  MemberPrefsRequest,
  MemberRef,
  MessagePage,
  RoomDetail,
  RoomKind,
  RoomListResponse,
  RoomMember,
  RoomSection,
  RoomSummary,
  RunStatusResponse,
} from "./messaging/types";
export { createRuntimeClient, RuntimeRpcError } from "./runtime/client";
export type {
  RuntimeApprovalDecision,
  RuntimeApprovalResolveRequest,
  RuntimeQuestionResolveRequest,
} from "./runtime/types";

export {
  isTextBlock,
  isThinkingBlock,
  isToolResultBlock,
  isToolUseBlock,
  resolveMessageBlocks,
  resolveToolArgs,
  resolveToolUseId,
} from "./chat/history";
export type { ContentBlock, HistoryMessage } from "./chat/history";
export { sessionIdFromKey } from "./chat/sessionKey";
