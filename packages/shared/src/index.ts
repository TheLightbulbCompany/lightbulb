/**
 * @lightbulb/shared — framework-neutral core for Lightbulb clients.
 *
 * Contents: adapter contract, API client core, the AppSync live-event hook,
 * and platform-parameterized data hooks.
 *
 * This barrel is the package's public surface: every line here is imported by
 * `apps/frontend`, `apps/mobile` or both. Internals stay internal — import them relatively from
 * inside the package instead of re-exporting them here.
 */
export type { PlatformAdapter } from "./adapter";

export type { RuntimeRpcSource } from "./hooks/useRuntimeRpc";
export { useChatGPTOAuth } from "./hooks/useChatGPTOAuth";
export type { OAuthState } from "./hooks/useChatGPTOAuth";
export { useStatusLoaded } from "./hooks/containerStatus";
export type {
  ClassifiedStatus,
  ContainerStatusBody,
} from "./hooks/containerStatus";
export { deriveUIPhase } from "./hooks/statusContract";
export type { StatusContract, UIPhase } from "./hooks/statusContract";
export { refreshOnboardingStatus, resetSwrCache, useOnboardingStatus, useResetSwrOnUserChange } from "./hooks/useOnboardingStatus";
export type { VisibilityAdapter } from "./hooks/useOnboardingStatus";

export { ApiError } from "./api/error";
export { createApiClient, fetchJsonWithStatus } from "./api/client";
export type { ApiCallOptions, ApiMethods } from "./api/client";

export { accumulateStreamText } from "./live/streamText";
export { AppSyncEventsClient, framesChannelFor } from "./live/appsyncEvents";
export { useLiveEvents } from "./live/useLiveEvents";
export type { CallTurnFrame, ChatIncomingMessage, LiveEvent, TaskChanged, LiveEventOf, MessagePush, RoomActivityChanged, RoomBrowserFrame, RoomSubagentEvent, SessionAttention, SessionBrowserFrame, SessionScreen, SessionTyping } from "./live/types";
export { createMessagingClient } from "./messaging/client";
export { PERSON_COLORS, personColor } from "./messaging/personColor";
export type { PersonColor } from "./messaging/personColor";
export type { MessagingClient, RoomAttachmentInput } from "./messaging/client";
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
export { ROOMS_SNAPSHOT_VERSION, roomsListKey, useRooms } from "./messaging/useRooms";
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
export type {
  CanonicalRoomMessage,
  CreateMessageRequest,
  CreateRoomRequest,
  MessageAttachment,
  MessageAttachmentContentType,
  ListenMode,
  LiveConfig,
  MemberPrefsRequest,
  PatchRoomRequest,
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
  UpsertRoomMemberRequest,
  ProjectColor,
  ProjectIcon,
} from "./messaging/types";
export {
  PROJECT_COLORS,
  PROJECT_ICONS,
  ROOM_TOPIC_MAX_CHARS,
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
  TASK_STATUSES, SHOWN_GROUPS, STATUS_WORDS, GROUP_LABELS, GROUP_ORDER, resolveCeoAgentId, taskView, summaryView, specialistLabel, nameSpecialistIn, activityLine, elapsedShort, overviewGroups, taskSummaryLine, effectLine, effectName, formatSchedule, timeOfDay,
  FREE_TEXT_OPTION, ALWAYS_ALLOW_UNAVAILABLE, answerVerb, askFields, askLine, decisionButtons, decisionMeta, moneyText, previewRows, primaryOptionId, stepLines, stepNow, chipState, clockTime,
} from "./tasks/view";
export type {
  AgentNames, ChipMark, DecisionButton, EffectRow, OverviewGroup, OverviewGroupId, QuestionRow, ShownGroup, StatusWord, SummaryView, TaskAction, TaskRow, TaskStatus, TaskTone, TaskView, TaskViewInput,
} from "./tasks/view";
export { OUTPUT_KINDS } from "./tasks/types";
export type {
  AnswerBody, CreationOutput, OutputKind, QuestionKind, QuestionOption, QuestionView, SummaryQuestion, TaskAttempt, TaskDetail, TaskEffect, TaskOutput,
  AutomationView, TaskQuestionRow, TaskRef, TaskScope, TaskSubscription, TaskSummary, TaskTrace, TaskVerb,
} from "./tasks/types";
export { createTasksClient, scopeParam } from "./tasks/client";
export type { OutputsFilter, TasksClient } from "./tasks/client";
export type { DirectoryAgent } from "./tasks/useTasks";
export { NEEDS_YOU_KEY_SUFFIX, putTaskSummary, questionsKey, refreshTask, taskKey, taskListKey, agentDirectory, useAgentDirectory, useTask, useOlderTasks, useTaskCache, useTaskChangedRefresh, useTaskList, useTaskQuestions } from "./tasks/useTasks";
export {
  automationRowLine, automationStatusLine, budgetLine, canDecide, canEdit, deliveryRoomId, deliveryWords, money, nextRunWords, runNow, runNowReason, runsAgain, runsPerMonth, scheduleWords, viewerTimeZone, waitingWords,
} from "./tasks/automations";
export type { RunNowOutcome } from "./tasks/automations";
export {
  automationKey, automationRunsKey, automationsKey, useAutomation, useAutomationRuns, useAutomations,
} from "./tasks/useAutomations";
export type { AutomationSource } from "./tasks/useAutomations";

export { PHONE_VOICES, phoneVoice, voiceSamplePath } from "./voicePreferences";
export type { PhoneVoice, SampleLanguage } from "./voicePreferences";
