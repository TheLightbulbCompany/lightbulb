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
export { NEW_SCREEN, SNAPSHOT_WAIT_MS, receiveScreen, settleScreen, snapshotWaits, type ScreenOrder } from "./live/sessionScreen";
export { chunkInput, createInputQueue, dropMouseReports, dropProbeReplies, inputRefused, MAX_INPUT_BYTES, SHIFT_ENTER, toBase64 } from "./live/sessionInput";

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
export type { OAuthState } from "./hooks/useChatGPTOAuth";
export { useStatusLoaded } from "./hooks/containerStatus";
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
export { refreshOnboardingStatus, resetSwrCache, useOnboardingStatus } from "./hooks/useOnboardingStatus";
export type { VisibilityAdapter } from "./hooks/useOnboardingStatus";

export { ApiError } from "./api/error";
export { createApiClient, fetchJsonWithStatus } from "./api/client";
export type { ApiCallOptions, ApiMethods } from "./api/client";

export { accumulateStreamText } from "./live/streamText";
export { AppSyncEventsClient, framesChannelFor } from "./live/appsyncEvents";
export { useLiveEvents } from "./live/useLiveEvents";
export type { CallTurnFrame, ChatIncomingMessage, LiveEvent, TaskChanged, LiveEventOf, MessagePush, RoomActivityChanged, RoomBrowserFrame, RoomSubagentEvent, SessionAttention, SessionBrowserFrame, SessionScreen, SessionTyping } from "./live/types";
export { applyRoomActivity, pruneRoomActivity, roomLiveLine, typingLine } from "./messaging/roomActivity";
export type { RoomActivity } from "./messaging/roomActivity";
export { createMessagingClient } from "./messaging/client";
export { useSubagentTail } from "./messaging/useSubagentTail";
export { useSessionSearch } from "./messaging/useSessionSearch";
export type { SearchState } from "./messaging/useSessionSearch";
export { PERSON_COLORS, personColor } from "./messaging/personColor";
export type { PersonColor } from "./messaging/personColor";
export type { MessagingClient, RoomAttachmentInput, SubagentTail } from "./messaging/client";
export { wrapSelection } from "./messaging/composerText";
export type { MarkdownStyle } from "./messaging/composerText";
export { canChangeListenMode, LISTEN_MODE_LABELS, listenModeOptions, respondHint, soleChannelOwner } from "./messaging/respondHint";
export {
  buildSetupSteps,
  isBandComplete,
  isConnectorStep,
  setupProgress,
  ENDOWED_STEPS,
  CONNECTOR_STEPS,
  STARTER_AGENT_DEFAULT_NAMES,
} from "./messaging/onboardingSteps";
export type {
  SetupStep,
  SetupStepId,
  SetupStepInputs,
  SetupParticipant,
  SetupConnection,
  SetupBilling,
} from "./messaging/onboardingSteps";
export type { RespondTarget } from "./messaging/respondHint";
export {
  GROUP_WINDOW_MS,
  browsingSite,
  buildTimeline,
  dateDividerLabel,
  frameText,
  lastSequence,
  mergeCanonicalMessages,
  roomDisplayName,
  runView,
  collapseRuns,
  placeRuns,
} from "./messaging/timeline";
export type {
  LiveRoomRun,
  RoomRunView,
  RunToolCall,
  TimelineItem,
} from "./messaging/timeline";
export {
  collapseRepeats,
  redactToolArgs,
  resolveToolDisplay,
  summarizeToolRun,
  toolRowLabel,
} from "./messaging/toolDisplay";
export type { ToolDisplay, ToolKind } from "./messaging/toolDisplay";
export {
  MENTION_SCHEME,
  activeTrigger,
  applyCompletion,
  linkifyMentions,
  linkifySessionMentions,
  displaySessionMentions,
  parseMentionHref,
  resolveTypedMentions,
  resolveTypedSessionMentions,
  sessionMentionCandidates,
  sessionMentionHandle,
} from "./messaging/mentions";
export type { CompletionTrigger, MentionTarget, SessionMentionCandidate, TriggerKind } from "./messaging/mentions";
export { ALL_EMOJI, EMOJI_CATEGORIES, searchEmoji } from "./messaging/emoji";
export type { EmojiEntry } from "./messaging/emoji";
export { MAX_LINK_PREVIEWS, extractPreviewUrls, linkDomain, linkPreviewPath, useLinkPreview } from "./messaging/linkPreview";
export type { LinkPreview } from "./messaging/linkPreview";
export {
  DOCUMENT_LINK_RE,
  MAX_DOCUMENT_CARDS,
  documentPreviewText,
  extractDocumentLinks,
  isDocumentLink,
  useDocumentPreview,
} from "./messaging/documentLinks";
export type { DocumentLink, DocumentPreview } from "./messaging/documentLinks";
export { ROOMS_SNAPSHOT_VERSION, useRooms } from "./messaging/useRooms";
export type {
  MessageDelegations,
  OptimisticRoomMessage,
  RoomReceipts,
  RoomsLiveSource,
  RoomsPersistence,
  RoomsPlatform,
  RoomsSnapshot,
  SendState,
  TypingMember,
  UseRoomsOptions,
  UseRoomsResult,
} from "./messaging/useRooms";
export {
  activityActionRequest,
  attentionActivityCount,
  flattenActivityPages,
  newestActivityAt,
  replaceActivityItem,
  unseenActivityCount,
  useActivity,
} from "./messaging/useActivity";
export type { ActivityClient, ActivityPlatform, UseActivityOptions, UseActivityResult } from "./messaging/useActivity";
export { ideaSectionKey, ideaSections, orderIdeas, useIdeas } from "./messaging/ideas";
export type { Idea, IdeaSection, IdeaSectionKey, IdeaSize, IdeaStart, IdeasClient, IdeasResponse } from "./messaging/ideas";
export {
  ACTIVITY_EMPTY_COPY,
  ACTIVITY_FILTERS,
  activityRowParts,
  approvalActivityId,
  interactionActivityId,
  approvalNotificationCopy,
  filterActivity,
  groupActivity,
  isNeedsYou,
} from "./messaging/activityView";
export type { ActivityFilter, ActivityRowParts, ActivitySection } from "./messaging/activityView";
export {
  NOTIFICATION_LINE_CHARS,
  notificationLine,
  notificationTitle,
  roomWhere,
  runFailureLine,
  sentenceCase,
} from "./messaging/notificationCopy";
export {
  GLYPH,
  sessionGlyph,
  PREVIEW_LINES,
  buildRows,
  diffLines,
  fullOutput,
  lifecycleLabel,
  mainEvents,
  sessionSubagents,
  subagentStatus,
  subagentState,
  subagentStrip,
  sessionClock,
  backgroundWaiting,
  subagentTags,
  stripAnsi,
  subagentElapsedMs,
  subagentLabel,
  groupRows,
  numberedDiff,
  inputDiff,
  approvalInputView,
  hasHiddenChars,
  canonicalJson,
  pendingApproval,
  pendingQuestion,
  questionKeys,
  thinkingLabel,
  toolActivity,
  toolGroupSummary,
  toolHeader,
  toolRowView,
} from "./messaging/localSessionTranscript";
export type {
  ApprovalInputView,
  DiffLine,
  LocalSessionProvider,
  SessionSubagent,
  StripRow,
  ThinkingTranscriptRow,
  ToolActivity,
  ToolGroupItem,
  ToolRowView,
  ToolTranscriptRow,
  TranscriptItem,
  SessionQuestion,
  TurnChanges,
  TranscriptRow,
} from "./messaging/localSessionTranscript";
export { sessionChanges, sessionPlan } from "./messaging/localSessionOverview";
export type { FileChange, PlanStatus, PlanStep } from "./messaging/localSessionOverview";
export { accessLost, useHistorySource, useLocalSessionHistory } from "./messaging/useLocalSessionHistory";
export type { SessionSource } from "./messaging/useLocalSessionHistory";
export { useWorkspaceSessions } from "./messaging/useWorkspaceSessions";
export { canSendToSession, endedWhy, markSessionRead, markSessionsSeen, markSessionUnread, sendBlockedReason, SESSION_ARCHIVE_AFTER_MS, SIDEBAR_SESSION_CAP, sessionArchived, sessionArchivedByViewer, groupByDay, sessionEndable, sessionHistory, sessionLabel, sessionLive, sessionMatches, sessionState, sessionStateAction, sessionStateLabel, sessionStatus, sessionTitle, sessionWaitingLine, sessionsNeedingYou, sidebarSessionList, SESSION_NOTIFICATION_DEFAULTS, type NotifyLevel, type SessionNotifications, sessionToolName, changesLabel, sessionAgentsLabel, sessionPrLabel, sessionSignalsLine, sessionAgentName, SESSION_AGENTS, sessionUnread, sessionViewKey, unavailableReason, workspaceSections } from "./messaging/localSessionLabels";
export type { SessionDayGroup, SessionMarks, SessionSeen, SessionState, WorkspaceSection } from "./messaging/localSessionLabels";
export { mergeSessionRows } from "./messaging/sessionRow";
// `SessionState` is already the label state above; the row's is exported under its own name.
export type { EngineRecord, SessionRow, SessionState as SessionRowState, SessionViewer } from "./messaging/sessionRow";
export { CLI_ONLY, conversationOnly, sessionScreenNotice } from "./messaging/sessionStates";
export { agentRingState, mergeChats, roomActivityAt, roomPreview, sessionActivityAt, sessionPreview, sessionRingState } from "./messaging/chatList";
export type { AvatarRing, ChatEntry, MergeChatsOptions } from "./messaging/chatList";
export { lastOwnMessageId, readState, readStateLabel, UNPROMPTED_LABEL, unpromptedLabel } from "./messaging/readState";
export type { ReadState } from "./messaging/readState";
export { AUTO_CHECK_INTERVAL_MS, COMMAND_MAX_BYTES, pendingPrompt, receiptChip, useSessionCommand, usePendingPrompt, useSessionOutbox, type SessionReceiptChip } from "./messaging/useSessionCommand";
export { APPROVAL_MODE_LABEL, approvalAgent, approvalModeAction, ASK_AGAIN_AFTER_MS, audienceLine, auditLine, COMMIT_AND_PR_INSTRUCTION, canControlSession, canResumeSession, pickupRequestLine, watchOnlyLine, pickupSource, pickupStatus, resumeStatus, sessionFacts, sessionFooter, usageLine, useSessionApproval, useSessionQuestion, useSessionAudit, useSessionJoinRequest, useSessionResume, useShownInputMatches, useTeamControl, useUsageSharing, withAuditNotices } from "./messaging/sessionControls";
export type { ApprovalAnswer, SessionControlClient, SessionResumeFlow } from "./messaging/sessionControls";
export type { SessionCommandAttempt } from "./messaging/useSessionCommand";
export type {
  LocalSession,
  SessionChanges,
  SessionPr,
  LocalSessionApprovalMode,
  LocalSessionAuditEntry,
  LocalSessionPendingApproval,
  LocalSessionPendingQuestion,
  SessionCopyCode,
  SessionLinkViewing,
  SessionLink,
  SessionLinkAudience,
  SessionLinkCode,
  SessionLinkPage,
  SessionLinkJoinAsk,
  LocalSessionPickup,
  LocalSessionPickupOutcome,
  LocalSessionPickupPart,
  LocalSessionPickupTicket,
  LocalSessionResume,
  LocalSessionCommand,
  LocalSessionCommandReceipt,
  LocalSessionOfflineOutbox,
  LocalSessionEvent,
  LocalSessionEventActor,
  LocalSessionEventTool,
  LocalSessionLifecycle,
  LocalSessionEventPage,
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
  MessageSearchHit,
  MessageSearchResponse,
  SessionSearchHit,
  SessionSearchResponse,
  ActiveRoomRun,
  RoomDetail,
  RoomKind,
  RoomListResponse,
  RoomMember,
  RoomSection,
  RoomSummary,
  RunStatusResponse,
  UpsertRoomMemberRequest,
  ActivityAction,
  ActivityActionInput,
  ActivityActionRequest,
  ActivityActor,
  ActivityItem,
  ActivityKind,
  ActivityLinkKind,
  ActivityPage,
  ActivitySeenResponse,
  ActivitySubtype,
  ActivitySummary,
  FeedResponse,
} from "./messaging/types";
export {
  MESSAGE_ATTACHMENT_ACCEPT,
  MESSAGE_ATTACHMENT_CONTENT_TYPES,
  MESSAGE_ATTACHMENT_MAX_BYTES,
  MESSAGE_ATTACHMENT_MAX_COUNT,
  MESSAGE_ATTACHMENT_UPLOAD_CONTENT_TYPES,
  isVideoAttachmentType,
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
export * from "./voice/words";
export {
  EMPTY_ROOM_SUBAGENTS,
  applySubagentEvent,
  liveSubagents,
  pruneFinishedSubagents,
  reconcileSubagentSnapshot,
  subagentActivity,
  isSubagentGone,
  delegationIdOf,
} from "./messaging/subagents";
export type { RoomSubagents, SubagentProgress, SubagentStatus } from "./messaging/subagents";
export { CARD_FENCE, CARD_MAX_BYTES, CARD_MAX_OPTIONS, CARD_ROOT } from "./ui/constants";
// Type only: the vocabulary (and OpenUI's parser) is `@lightbulb/shared/cards`, so the
// barrel never loads lang-core.
export type { CardName } from "./ui/library";
export { cardReplyRequest, choiceReply, formReply, tableReply } from "./ui/reply";
export type { CardReply } from "./ui/reply";
export { answerIsFormOf, answerIsFrom, answerIsTableOf, cardAnswerFor, cardAnswersFor } from "./ui/answer";
export {
  TASK_STATUSES, SHOWN_GROUPS, GROUP_LABELS, GROUP_ORDER, resolveCeoAgentId, taskView, summaryView, specialistLabel, overviewGroups, taskSummaryLine, effectLine, formatSchedule,
} from "./tasks/view";
export type {
  EffectRow, OverviewGroup, QuestionRow, ShownGroup, SummaryView, TaskAction, TaskRow, TaskStatus, TaskTone, TaskView, TaskViewInput,
} from "./tasks/view";
export { OUTPUT_KINDS } from "./tasks/types";
export type {
  AnswerBody, OutputKind, QuestionKind, QuestionOption, QuestionView, SummaryQuestion, TaskAttempt, TaskDetail, TaskEffect,
  AutomationView, TaskQuestionRow, TaskRef, TaskScope, TaskSubscription, TaskSummary, TaskTrace, TaskVerb,
} from "./tasks/types";
export { createTasksClient, scopeParam } from "./tasks/client";
export type { TasksClient } from "./tasks/client";
export { putTaskSummary, questionsKey, refreshTask, taskKey, taskListKey, useTask, useTaskCache, useTaskChangedRefresh, useTaskList, useTaskQuestions } from "./tasks/useTasks";
