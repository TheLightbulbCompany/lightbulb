/**
 * @lightbulb/shared — framework-neutral core for Lightbulb clients.
 *
 * Contents: adapter contract, API client core, the AppSync live-event hook,
 * and platform-parameterized data hooks.
 *
 * This barrel is the package's public surface: every line here is imported by
 * `apps/frontend`, `apps/mobile` or both — with one deliberate exception that
 * has no consumer yet and must NOT be pruned: everything originating in
 * `messaging/{useRooms,timeline,mentions}.ts`, held for the mobile port onto the
 * shared room machine. Internals stay internal — import them relatively from
 * inside the package instead of re-exporting them here.
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
export { useHiddenOnAgentCore, useRuntimeHost } from "./hooks/containerStatus";
export type {
  AgentCoreGate,
  ClassifiedStatus,
  ContainerStatusBody,
  RuntimeHost,
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
export type { ChatIncomingMessage, LiveEvent, LiveEventOf } from "./live/types";
export { createMessagingClient } from "./messaging/client";
export { wrapSelection } from "./messaging/composerText";
export type { MarkdownStyle } from "./messaging/composerText";
export { LISTEN_MODE_LABELS, listenModeOptions, respondHint } from "./messaging/respondHint";
export type { RespondTarget } from "./messaging/respondHint";
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
export { ALL_EMOJI, EMOJI_CATEGORIES, searchEmoji } from "./messaging/emoji";
export type { EmojiEntry } from "./messaging/emoji";
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
export {
  GLYPH,
  buildRows,
  diffLines,
  fullOutput,
  lifecycleLabel,
  stripAnsi,
  toolHeader,
  toolRowView,
} from "./messaging/localSessionTranscript";
export type {
  LocalSessionProvider,
  ToolRowView,
  ToolTranscriptRow,
  TranscriptRow,
} from "./messaging/localSessionTranscript";
export { accessLost, useLocalSessionHistory, useRoomResources } from "./messaging/useRoomResources";
export type { RoomResourceSource } from "./messaging/useRoomResources";
export { useWorkspaceSessions } from "./messaging/useWorkspaceSessions";
export { canSendToSession, sessionLabel, sessionStatus, sessionViewKey, unavailableReason, workspaceSections } from "./messaging/localSessionLabels";
export type { WorkspaceSection } from "./messaging/localSessionLabels";
export { AUTO_CHECK_INTERVAL_MS, COMMAND_MAX_BYTES, receiptCopy, useSessionCommand } from "./messaging/useSessionCommand";
export type { SessionCommandAttempt } from "./messaging/useSessionCommand";
export type {
  LocalSession,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
  LocalSessionEvent,
  LocalSessionEventActor,
  LocalSessionEventTool,
  LocalSessionLifecycle,
  LocalSessionEventPage,
  RoomResources,
  WorkspaceSessions,
  CanonicalRoomMessage,
  CreateMessageRequest,
  CreateRoomRequest,
  UpdateMessageRequest,
  MessageAttachment,
  MessageAttachmentContentType,
  ListenMode,
  LiveConfig,
  MemberPrefsRequest,
  MemberRef,
  MessagePage,
  ActiveRoomRun,
  RoomDetail,
  RoomKind,
  RoomListResponse,
  RoomMember,
  RoomSection,
  RoomSummary,
  RunStatusResponse,
} from "./messaging/types";
export {
  MESSAGE_ATTACHMENT_ACCEPT,
  MESSAGE_ATTACHMENT_CONTENT_TYPES,
  MESSAGE_ATTACHMENT_MAX_BYTES,
  MESSAGE_ATTACHMENT_MAX_COUNT,
  resolveMessageAttachmentContentType,
} from "./messaging/types";
export { createRuntimeClient, RuntimeRpcError } from "./runtime/client";

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
