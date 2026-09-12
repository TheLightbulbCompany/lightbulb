import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  accumulateStreamText,
  type CanonicalRoomMessage,
  type ChatIncomingMessage,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActionSheetIOS,
  ActivityIndicator,
  Alert,
  Animated,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { AgentHead } from "../../../components/chat/AgentHead";
import { AttachmentChips } from "../../../components/chat/AttachmentChips";
import { ChatErrorCard } from "../../../components/chat/ChatErrorCard";
import type { ChatItem } from "../../../components/chat/chatItems";
import { EmptyState } from "../../../components/chat/EmptyState";
import { MessageMarkdown } from "../../../components/chat/MessageMarkdown";
import { TimelineSkeleton } from "../../../components/chat/TimelineSkeleton";
import { ToolPill, type ToolPillStatus } from "../../../components/chat/ToolPill";
import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing } from "../../../config/theme";
import { setCurrentAgentId } from "../../../lib/activeAgent";
import {
  buildUploadNotice,
  filterOversized,
  uploadAttachments,
  MAX_ATTACHMENTS,
  type PendingAttachment,
} from "../../../lib/attachments";
import { useRooms } from "../../../lib/rooms";
import { useLiveEvents } from "../../../providers/live-events";

const ABORTED_PATTERN = /Agent run was cancelled/i;
const ERROR_PATTERNS: [RegExp, string][] = [
  [/timed out during opening handshake/i, "Your agent is starting up — please try again in a moment."],
  [/Gateway not healthy/i, "Your agent is not ready yet. Please try again in a moment."],
  [/health check timed out/i, "Your agent is not responding. Please try again in a moment."],
  [/Gateway connection lost/i, "Lost connection to your agent. Retrying..."],
  [/Connection closed/i, "Connection to your agent was interrupted. Please try again."],
  [/session file locked/i, "Your agent is busy with another request. Please wait a moment and try again."],
  [/Model access is denied/i, "This model is not available. Try switching to a different model in settings."],
];

function friendlyError(raw: string): string {
  return ERROR_PATTERNS.find(([pattern]) => pattern.test(raw))?.[1] ?? raw;
}

function ReconnectingBanner() {
  const pulse = usePulse(true, 0.3, 700);
  return (
    <View style={styles.banner}>
      <Animated.View style={[styles.bannerDot, { opacity: pulse }]} />
      <Text style={styles.bannerText}>
        Reconnecting live updates. You can keep sending.
      </Text>
    </View>
  );
}

function messageItem(message: CanonicalRoomMessage, userId: string | null): ChatItem {
  return {
    id: message.messageId,
    role: message.author.kind === "human" && message.author.id === userId
      ? "user"
      : "assistant",
    text: message.text,
    runId: message.runId ?? undefined,
  };
}

export default function AgentChatRoute() {
  const { agentId } = useLocalSearchParams<{ agentId: string }>();
  return <AgentChat key={agentId ?? "none"} />;
}

function AgentChat() {
  const { agentId, name } = useLocalSearchParams<{ agentId: string; name?: string }>();
  const { onEvent } = useLiveEvents();
  const { userId, getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  // Room resolve-or-create, history reconciliation, the live-event fan-out,
  // optimistic sends and mark-read all live in @lightbulb/shared's useRooms.
  // What stays here is the streaming-run projection: the shared hook keeps runs
  // as LiveRoomRun objects, this screen renders frames as tool pills and
  // resyncable assistant segments.
  const {
    activeRoom,
    accessRemoved,
    messages,
    optimisticMessages,
    historyErrorMessage,
    roomsError,
    directoryError,
    roomError,
    selectRoomError,
    httpReady,
    liveConnected,
    loadHistory,
    refreshRooms,
    selectAgentDm,
    sendMessage,
    reconcileSend,
    sendState,
    abortRun,
  } = useRooms(agentId ?? null);
  const activeRoomId = activeRoom?.roomId ?? null;
  const loadHistoryRef = useRef(loadHistory);
  loadHistoryRef.current = loadHistory;

  useFocusEffect(
    useCallback(() => {
      setCurrentAgentId(agentId ?? null);
      return () => setCurrentAgentId(null);
    }, [agentId]),
  );

  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [inputFocused, setInputFocused] = useState(false);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [sizeError, setSizeError] = useState<string | null>(null);
  const sizeErrorTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const drainedRunIdsRef = useRef(new Set<string>());
  const committedOffsetRef = useRef(0);
  const segmentRef = useRef(0);
  const lastCumulativeLenRef = useRef(0);
  const cumulativeTextRef = useRef<string | null>(null);
  const listRef = useRef<FlatList<ChatItem>>(null);
  const inputRef = useRef<TextInput>(null);
  const pendingSendRef = useRef<{ clientMessageId: string; text: string } | null>(null);

  // The shared hook resets its own message/optimistic state on a room change;
  // the live-stream projection this screen owns has to be reset alongside it.
  useEffect(() => {
    setItems([]);
    setBusy(false);
    activeRunIdRef.current = null;
  }, [activeRoomId]);

  // A run the canonical transcript now covers is done double-duty: drop its
  // live items so the stream doesn't render twice (sessions contract §5).
  //
  // Scoped to the messages that just ARRIVED, which is what the old
  // `applyCanonical(batch)` pruned on. Two failure modes bracket this:
  // filtering on every runId in `messages` kills a still-streaming run,
  // because `reconcile` re-merges into a fresh array after every page (a
  // reconnect or the `run_ended` refetch is enough, even with an empty page);
  // remembering runIds already pruned kills the dedupe instead, because the
  // user's own message carries the runId of the run it started, so the run
  // would be marked handled long before the agent's reply lands. Tracking
  // seen message ids is the only cut that survives both.
  const seenMessageIdsRef = useRef(new Set<string>());
  useEffect(() => {
    seenMessageIdsRef.current = new Set();
  }, [activeRoomId]);
  useEffect(() => {
    const arrivedRunIds = new Set<string>();
    for (const message of messages) {
      if (seenMessageIdsRef.current.has(message.messageId)) continue;
      seenMessageIdsRef.current.add(message.messageId);
      if (message.runId) arrivedRunIds.add(message.runId);
    }
    if (arrivedRunIds.size === 0) return;
    setItems((current) => {
      const next = current.filter((item) => !item.runId || !arrivedRunIds.has(item.runId));
      return next.length === current.length ? current : next;
    });
  }, [messages, activeRoomId]);

  const handleFrame = useCallback((msg: ChatIncomingMessage, runId: string) => {
    if (msg.type === "run_active") {
      if (msg.active) {
        activeRunIdRef.current = runId;
        setBusy(true);
      } else {
        activeRunIdRef.current = null;
        setBusy(false);
      }
      return;
    }
    if (
      activeRunIdRef.current === null &&
      !drainedRunIdsRef.current.has(runId) &&
      msg.type !== "run_ended"
    ) {
      activeRunIdRef.current = runId;
      committedOffsetRef.current = 0;
      segmentRef.current = 0;
      lastCumulativeLenRef.current = 0;
      cumulativeTextRef.current = null;
      setBusy(true);
    }
    if (activeRunIdRef.current !== runId && msg.type !== "error") return;

    if (msg.type === "chunk") {
      const segmentId = `${runId}-s${segmentRef.current}`;
      const cumulative = msg.append
        ? accumulateStreamText(cumulativeTextRef.current, msg)
        : (cumulativeTextRef.current ?? "").slice(0, committedOffsetRef.current) + msg.content;
      cumulativeTextRef.current = cumulative;
      const segmentText = cumulative.slice(committedOffsetRef.current);
      lastCumulativeLenRef.current = cumulative.length;
      setItems((current) => {
        const index = current.findIndex((item) => item.id === segmentId);
        if (index < 0) {
          return [...current, { id: segmentId, role: "assistant", text: segmentText, streaming: true, runId }];
        }
        const next = [...current];
        next[index] = { ...next[index], text: segmentText };
        return next;
      });
    } else if (msg.type === "tool_start") {
      committedOffsetRef.current = lastCumulativeLenRef.current;
      segmentRef.current += 1;
      setItems((current) => [
        ...current.map((item) => item.streaming ? { ...item, streaming: false } : item),
        {
          id: msg.toolCallId ?? randomUUID(),
          role: "tool",
          text: `⚙ ${msg.tool}`,
          tool: msg.tool,
          args: msg.args,
          toolStatus: "running",
          runId,
        },
      ]);
    } else if (msg.type === "tool_progress") {
      const progress = (msg.partialResult ?? []).map((block) => block.text ?? "").join("\n");
      setItems((current) => current.map((item) =>
        item.role === "tool" && item.id === msg.toolCallId
          ? { ...item, result: progress || item.result }
          : item,
      ));
    } else if (msg.type === "command_output") {
      setItems((current) => current.map((item) =>
        item.role === "tool" && item.id === msg.toolCallId
          ? { ...item, result: `${item.result ?? ""}${msg.output ?? ""}` }
          : item,
      ));
    } else if (msg.type === "tool_end" || msg.type === "tool_error") {
      const result = (msg.result ?? []).map((block) => block.text ?? "").filter(Boolean).join("\n");
      setItems((current) => current.map((item) =>
        item.role === "tool" && item.id === msg.toolCallId
          ? {
              ...item,
              toolStatus: msg.type === "tool_error" ? "error" : "done",
              result: result || msg.meta || item.result,
            }
          : item,
      ));
    } else if (msg.type === "error") {
      if (msg.code === "agent_busy") return;
      const calm = msg.code === "container_waking" || ABORTED_PATTERN.test(msg.message);
      setItems((current) => [
        ...current,
        {
          id: randomUUID(),
          role: calm ? "assistant" : "error",
          text: friendlyError(msg.message),
          code: calm ? undefined : msg.code,
        },
      ]);
      activeRunIdRef.current = null;
      setBusy(false);
    } else if (msg.type === "run_ended") {
      drainedRunIdsRef.current.add(runId);
      setItems((current) => current.map((item) => item.streaming ? { ...item, streaming: false } : item));
      activeRunIdRef.current = null;
      setBusy(false);
      loadHistoryRef.current();
    }
  }, []);

  // Every other branch of the live stream is the shared hook's; this one is
  // not, because only this screen turns frames into ChatItems.
  useEffect(() => onEvent((event) => {
    if (
      event.type === "room.run.frame" &&
      event.payload.roomId === activeRoomId &&
      event.payload.agentId === agentId
    ) {
      handleFrame(event.payload.frame, event.payload.runId);
    }
  }), [activeRoomId, agentId, onEvent, handleFrame]);

  const showSizeError = useCallback((message: string) => {
    if (sizeErrorTimerRef.current) clearTimeout(sizeErrorTimerRef.current);
    setSizeError(message);
    sizeErrorTimerRef.current = setTimeout(() => {
      setSizeError(null);
      sizeErrorTimerRef.current = null;
    }, 5_000);
  }, []);
  const dismissSizeError = useCallback(() => {
    if (sizeErrorTimerRef.current) clearTimeout(sizeErrorTimerRef.current);
    sizeErrorTimerRef.current = null;
    setSizeError(null);
  }, []);
  useEffect(() => () => {
    if (sizeErrorTimerRef.current) clearTimeout(sizeErrorTimerRef.current);
  }, []);

  const addAttachments = useCallback((candidates: PendingAttachment[]) => {
    const { valid, error } = filterOversized(candidates);
    if (error) showSizeError(error);
    if (valid.length > 0) {
      setAttachments((current) => [...current, ...valid].slice(0, MAX_ATTACHMENTS));
    }
  }, [showSizeError]);
  const pickFromLibrary = useCallback(async () => {
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ["images", "videos"],
      allowsMultipleSelection: true,
      selectionLimit: MAX_ATTACHMENTS,
      quality: 1,
    });
    if (result.canceled) return;
    addAttachments(result.assets.map((asset) => ({
      id: randomUUID(),
      name: asset.fileName ?? asset.uri.split("/").pop() ?? "photo",
      size: asset.fileSize ?? 0,
      uri: asset.uri,
      mimeType: asset.mimeType ?? "application/octet-stream",
    })));
  }, [addAttachments]);
  const pickFiles = useCallback(async () => {
    const result = await DocumentPicker.getDocumentAsync({ multiple: true, copyToCacheDirectory: true });
    if (result.canceled) return;
    addAttachments(result.assets.map((asset) => ({
      id: randomUUID(),
      name: asset.name,
      size: asset.size ?? 0,
      uri: asset.uri,
      mimeType: asset.mimeType ?? "application/octet-stream",
    })));
  }, [addAttachments]);
  const openAttachSheet = useCallback(() => {
    const onError = (error: unknown) => console.error("attachment pick failed", error);
    if (Platform.OS === "ios") {
      ActionSheetIOS.showActionSheetWithOptions(
        { options: ["Photo Library", "Files", "Cancel"], cancelButtonIndex: 2 },
        (index) => {
          if (index === 0) void pickFromLibrary().catch(onError);
          else if (index === 1) void pickFiles().catch(onError);
        },
      );
    } else {
      Alert.alert("Attach", undefined, [
        { text: "Photo Library", onPress: () => void pickFromLibrary().catch(onError) },
        { text: "Files", onPress: () => void pickFiles().catch(onError) },
        { text: "Cancel", style: "cancel" },
      ]);
    }
  }, [pickFromLibrary, pickFiles]);

  const dispatchText = useCallback(async (text: string): Promise<boolean> => {
    if (!text || !activeRoomId || !agentId || !httpReady) return false;
    let pending = pendingSendRef.current;
    if (!pending || pending.text !== text) {
      pending = { clientMessageId: randomUUID(), text };
      pendingSendRef.current = pending;
    }
    try {
      // An attempt that failed without a server verdict may already be
      // persisted: reconcile first, then replay the SAME clientMessageId so the
      // server dedupes instead of double-posting.
      const canonical = sendState(pending.clientMessageId) === "uncertain"
        ? await reconcileSend(pending.clientMessageId)
        : await sendMessage({
            clientMessageId: pending.clientMessageId,
            text,
            mentions: [],
            replyToMessageId: null,
            threadRootMessageId: null,
          });
      // `reconcileSend` resolves undefined when the room changed under it and
      // the shared hook dropped the request — nothing was sent, so parking the
      // composer on a Stop button with no run to stop would be a dead end.
      if (!canonical) return false;
      pendingSendRef.current = null;
      if (canonical.runId) activeRunIdRef.current = canonical.runId;
      setBusy(true);
      return true;
    } catch (error) {
      setItems((current) => [
        ...current,
        {
          id: randomUUID(),
          role: "error",
          text: error instanceof Error ? error.message : "Send failed",
        },
      ]);
      return false;
    }
  }, [activeRoomId, agentId, httpReady, sendState, reconcileSend, sendMessage]);

  const send = useCallback(async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || uploading || !agentId || !activeRoomId) return;
    let message = text;
    if (attachments.length > 0) {
      setUploading(true);
      try {
        const uploaded = await uploadAttachments(
          () => getTokenRef.current(),
          agentId,
          attachments,
        );
        message = buildUploadNotice(uploaded) + message;
      } catch (error) {
        setItems((current) => [...current, {
          id: randomUUID(),
          role: "error",
          text: `File upload failed: ${error instanceof Error ? error.message : "Upload failed"}`,
        }]);
        return;
      } finally {
        setUploading(false);
      }
    }
    if (await dispatchText(message)) {
      setInput("");
      setAttachments([]);
    }
  }, [input, attachments, uploading, agentId, activeRoomId, dispatchText]);

  const stopRun = useCallback(async () => {
    const runId = activeRunIdRef.current;
    if (!runId || !httpReady) return;
    try {
      await abortRun(runId);
    } catch (error) {
      setItems((current) => [...current, {
        id: randomUUID(),
        role: "error",
        text: error instanceof Error ? error.message : "Stop failed",
      }]);
    }
  }, [httpReady, abortRun]);

  const agentName = name || agentId || "Agent";
  const allItems = useMemo(() => [
    ...messages.map((message) => messageItem(message, userId ?? null)),
    ...optimisticMessages.map((message) => ({
      id: `pending-${message.clientMessageId}`,
      role: "user" as const,
      text: message.text,
    })),
    ...items,
  ], [messages, optimisticMessages, items, userId]);
  // Once a room is open only its own detail error matters; before that, the
  // resolve-or-create errors do — `selectRoomError` last, since it reports the
  // failed create rather than the more specific fetch that caused it.
  // `accessRemoved` is not an error the user can retry past, so it leaves an
  // empty transcript, as it always has.
  const openError = roomError?.message
    ?? (activeRoom ? undefined : roomsError?.message ?? directoryError?.message ?? selectRoomError);
  const errorMessage = accessRemoved ? null : openError ?? historyErrorMessage;
  const roomLoading = !activeRoom && !errorMessage;
  let lastUserIndex = -1;
  for (let index = allItems.length - 1; index >= 0; index -= 1) {
    if (allItems[index].role === "user") {
      lastUserIndex = index;
      break;
    }
  }
  const showTyping = busy && !allItems.slice(lastUserIndex + 1).some((item) => item.role === "assistant");
  const hasDraft = !!input.trim() || attachments.length > 0;
  const lastItem = allItems.at(-1);
  let lastAnchorIndex = -1;
  for (let index = allItems.length - 1; index >= 0; index -= 1) {
    if (allItems[index].role === "user" || allItems[index].role === "error") {
      lastAnchorIndex = index;
      break;
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={90}
    >
      <Stack.Screen
        options={{
          title: agentName,
          headerLeft: () => (
            <Pressable
              style={styles.headerAction}
              onPress={() => router.canGoBack() ? router.back() : router.replace("/(app)/chat")}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Back to agents"
            >
              <Feather name="chevron-left" size={22} color={colors.ink} />
            </Pressable>
          ),
        }}
      />
      <FlatList
        ref={listRef}
        testID="chat-timeline"
        data={allItems}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[
          styles.list,
          allItems.length === 0 && !showTyping && !roomLoading && styles.listEmpty,
        ]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onContentSizeChange={() => listRef.current?.scrollToEnd({ animated: true })}
        ListHeaderComponent={errorMessage ? (
          <View style={[styles.errorCard, styles.historyErrorCard]}>
            <Text style={styles.errorText}>Couldn’t load history — {errorMessage}</Text>
            <Pressable
              onPress={() => {
                void refreshRooms().catch(() => {});
                // `initialAgentId` is one-shot per agent inside the hook, so a
                // failed resolve-or-create only retries if we re-drive it.
                if (agentId) void selectAgentDm(agentId).catch(() => {});
                loadHistory();
              }}
              accessibilityRole="button"
              accessibilityLabel="Retry loading history"
            >
              <Text style={styles.retryText}>Retry</Text>
            </Pressable>
          </View>
        ) : null}
        ListEmptyComponent={roomLoading ? <TimelineSkeleton /> : showTyping ? null : <EmptyState agentName={agentName} />}
        ListFooterComponent={showTyping ? (
          <View style={lastItem && (lastItem.role === "tool" ? styles.runGap : styles.groupGap)}>
            <AgentHead name={agentName} active />
          </View>
        ) : null}
        renderItem={({ item, index }) => {
          const previous = index > 0 ? allItems[index - 1] : undefined;
          const continuesGroup = previous != null && (previous.role === "assistant" || previous.role === "tool");
          if (item.role === "user") {
            return <View style={[styles.userBubble, index > 0 && styles.groupGap]}><Text style={styles.userText}>{item.text}</Text></View>;
          }
          if (item.role === "assistant") {
            let showHead = true;
            for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
              const role = allItems[cursor].role;
              if (role === "assistant") {
                showHead = false;
                break;
              }
              if (role === "user" || role === "error") break;
            }
            return (
              <View style={[styles.assistantGroup, index > 0 && (continuesGroup ? styles.runGap : styles.groupGap)]}>
                {showHead && <AgentHead name={agentName} active={busy && index > lastAnchorIndex} />}
                <MessageMarkdown text={item.text} />
              </View>
            );
          }
          if (item.role === "tool") {
            const rawStatus = item.toolStatus ?? "running";
            const status: ToolPillStatus = rawStatus === "running" && !busy ? "done" : rawStatus;
            return (
              <View style={index > 0 ? previous?.role === "tool" ? styles.pillGap : previous?.role === "assistant" ? styles.runGap : styles.groupGap : undefined}>
                <ToolPill tool={item.tool ?? item.text} status={status} args={item.args} result={item.result} />
              </View>
            );
          }
          return <View style={index > 0 ? styles.groupGap : undefined}><ChatErrorCard code={item.code} message={item.text} /></View>;
        }}
      />
      <View style={styles.composerOuter}>
        {!liveConnected && <ReconnectingBanner />}
        {sizeError && (
          <View style={styles.sizeErrorRow}>
            <Text style={styles.sizeErrorText}>{sizeError}</Text>
            <Pressable onPress={dismissSizeError} hitSlop={8} accessibilityRole="button" accessibilityLabel="Dismiss size error">
              <Feather name="x" size={12} color={colors.errorText} />
            </Pressable>
          </View>
        )}
        <View style={[styles.card, inputFocused && styles.cardFocused]}>
          <AttachmentChips attachments={attachments} onRemove={(id) => setAttachments((current) => current.filter((attachment) => attachment.id !== id))} />
          <View style={styles.inputZone}>
            <TextInput
              ref={inputRef}
              style={styles.input}
              placeholder="Ask anything"
              placeholderTextColor={colors.placeholder}
              value={input}
              onChangeText={setInput}
              multiline
              editable={httpReady && !uploading}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
            />
          </View>
          <View style={styles.rail}>
            <Pressable style={styles.attachButton} onPress={openAttachSheet} disabled={uploading} hitSlop={4} accessibilityRole="button" accessibilityLabel="Attach">
              <Feather name="plus" size={18} color={uploading ? colors.placeholder : colors.mute} />
            </Pressable>
            {busy ? (
              <Pressable
                style={[styles.sendButton, !httpReady && styles.sendDisabled]}
                onPress={() => void stopRun()}
                disabled={!httpReady}
                accessibilityRole="button"
                accessibilityLabel="Stop"
              >
                <Feather name="square" size={14} color={colors.white} />
              </Pressable>
            ) : (
              <Pressable
                style={[styles.sendButton, (!hasDraft || !httpReady) && styles.sendDisabled]}
                onPress={() => void send()}
                disabled={!hasDraft || uploading || !httpReady}
                accessibilityRole="button"
                accessibilityLabel="Send"
              >
                {uploading
                  ? <ActivityIndicator size="small" color={colors.white} />
                  : <Feather name="arrow-up" size={16} color={colors.white} />}
              </Pressable>
            )}
          </View>
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  list: { paddingHorizontal: spacing.lg, paddingTop: 32, paddingBottom: 32 },
  listEmpty: { flexGrow: 1 },
  groupGap: { marginTop: 40 },
  runGap: { marginTop: spacing.md },
  pillGap: { marginTop: spacing.sm },
  userBubble: {
    alignSelf: "flex-end",
    maxWidth: "85%",
    backgroundColor: colors.userBubble,
    borderRadius: radii.xl,
    borderBottomRightRadius: radii.sm,
    paddingHorizontal: spacing.lg,
    paddingVertical: 10,
  },
  userText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 28, color: colors.ink },
  assistantGroup: { alignSelf: "flex-start", maxWidth: "85%" },
  errorCard: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.lg,
  },
  errorText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.errorText },
  historyErrorCard: { marginBottom: spacing.lg, gap: spacing.sm },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
  headerAction: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm },
  composerOuter: { paddingHorizontal: spacing.lg, paddingBottom: spacing.lg, backgroundColor: colors.cream },
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.edge,
    shadowColor: colors.ink,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.05,
    shadowRadius: 16,
    elevation: 3,
  },
  cardFocused: {
    borderColor: colors.forestFocusBorder,
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.08,
    shadowRadius: 24,
    elevation: 4,
  },
  inputZone: { paddingHorizontal: spacing.lg, paddingTop: spacing.md },
  input: {
    fontFamily: fonts.sans,
    fontSize: 15,
    lineHeight: 24,
    maxHeight: 200,
    color: colors.ink,
    paddingTop: 0,
    paddingBottom: 0,
    textAlignVertical: "top",
  },
  rail: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.sm,
    paddingBottom: spacing.sm,
    paddingTop: 6,
  },
  attachButton: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
  },
  sendButton: {
    marginLeft: "auto",
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  sendDisabled: { opacity: 0.5 },
  sizeErrorRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  sizeErrorText: { flex: 1, fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.errorText },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.bannerBg,
    borderWidth: 1,
    borderColor: colors.bannerBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  bannerDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.connectingYellow },
  bannerText: { flex: 1, fontFamily: fonts.sansMedium, fontSize: 12, lineHeight: 16, color: colors.bannerText },
});
