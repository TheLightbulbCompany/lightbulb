import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  ApiError,
  accumulateStreamText,
  type CanonicalRoomMessage,
  type ChatIncomingMessage,
  type RoomDetail,
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

function mergeMessages(
  current: readonly CanonicalRoomMessage[],
  incoming: readonly CanonicalRoomMessage[],
): CanonicalRoomMessage[] {
  const byId = new Map(current.map((message) => [message.messageId, message]));
  for (const message of incoming) byId.set(message.messageId, message);
  return [...byId.values()].sort((left, right) => left.messageSeq - right.messageSeq);
}

function lastSequence(messages: readonly CanonicalRoomMessage[]): number {
  return messages.at(-1)?.messageSeq ?? 0;
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
  const live = useLiveEvents();
  const {
    httpReady,
    liveConnected,
    liveChannels,
    connectionRevision,
    messaging,
    claimActiveRoomSubscription,
    onEvent,
  } = live;
  const { userId, getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;

  useFocusEffect(
    useCallback(() => {
      setCurrentAgentId(agentId ?? null);
      return () => setCurrentAgentId(null);
    }, [agentId]),
  );

  const [room, setRoom] = useState<RoomDetail | null>(null);
  const [roomLoading, setRoomLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyNonce, setHistoryNonce] = useState(0);
  const roomCreationRef = useRef<{ agentId: string; clientRoomId: string } | null>(null);
  const [messages, setMessages] = useState<CanonicalRoomMessage[]>([]);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
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
  const generation = useRef(0);
  const reconciliation = useRef<Promise<void> | null>(null);
  const requestedAfter = useRef<number | null>(null);
  const pendingSendRef = useRef<{
    clientMessageId: string;
    text: string;
    uncertain: boolean;
  } | null>(null);

  const applyCanonical = useCallback((incoming: readonly CanonicalRoomMessage[]) => {
    if (!room) return;
    const valid = incoming.filter((message) => message.roomId === room.roomId);
    const merged = mergeMessages(messagesRef.current, valid);
    messagesRef.current = merged;
    setMessages(merged);
    const canonicalRuns = new Set(valid.flatMap((message) => message.runId ? [message.runId] : []));
    if (canonicalRuns.size > 0) {
      setItems((current) => current.filter((item) => !item.runId || !canonicalRuns.has(item.runId)));
    }
  }, [room]);

  useEffect(() => {
    if (!agentId || !httpReady) return;
    let cancelled = false;
    const resolveRoom = async () => {
      setRoomLoading(true);
      setHistoryError(null);
      try {
        const directory = await messaging.listRooms();
        const details = await Promise.all(
          directory.rooms
            .filter((candidate) => candidate.kind === "dm")
            .map((candidate) => messaging.getRoom(candidate.roomId).catch(() => null)),
        );
        let detail = details.find((candidate): candidate is RoomDetail =>
          !!candidate && candidate.members.some(
            (member) => member.kind === "agent" && member.id === agentId,
          ),
        );
        if (!detail) {
          const creation = roomCreationRef.current?.agentId === agentId
            ? roomCreationRef.current
            : { agentId, clientRoomId: randomUUID() };
          roomCreationRef.current = creation;
          detail = await messaging.createRoom({
            clientRoomId: creation.clientRoomId,
            kind: "dm",
            name: null,
            members: [{
              kind: "agent",
              id: agentId,
              role: "member",
              listenMode: "active",
            }],
          });
        }
        if (cancelled) return;
        setRoom(detail);
      } catch (error) {
        if (!cancelled) {
          setHistoryError(error instanceof Error ? error.message : "Failed to open conversation");
        }
      } finally {
        if (!cancelled) setRoomLoading(false);
      }
    };
    void resolveRoom();
    return () => {
      cancelled = true;
    };
  }, [agentId, httpReady, messaging, historyNonce]);

  useEffect(() => {
    generation.current += 1;
    messagesRef.current = [];
    setMessages([]);
    setItems([]);
    setBusy(false);
    activeRunIdRef.current = null;
    requestedAfter.current = null;
    reconciliation.current = null;
  }, [room?.roomId]);

  const subscriptionRoomId = room?.roomId;
  const subscriptionDeliveryKey = room?.deliveryKey;
  useFocusEffect(
    useCallback(() => {
      if (!subscriptionRoomId || !subscriptionDeliveryKey) return;
      return claimActiveRoomSubscription({
        roomId: subscriptionRoomId,
        deliveryKey: subscriptionDeliveryKey,
      });
    }, [subscriptionRoomId, subscriptionDeliveryKey, claimActiveRoomSubscription]),
  );

  const reconcile = useCallback((afterSeq: number) => {
    if (!room) return Promise.resolve();
    requestedAfter.current = requestedAfter.current === null
      ? afterSeq
      : Math.min(requestedAfter.current, afterSeq);
    if (reconciliation.current) return reconciliation.current;
    const roomId = room.roomId;
    const runGeneration = generation.current;
    const run = async () => {
      setHistoryError(null);
      while (requestedAfter.current !== null && generation.current === runGeneration) {
        let cursor = requestedAfter.current;
        requestedAfter.current = null;
        for (;;) {
          const page = await messaging.listMessages(roomId, cursor, 100);
          if (generation.current !== runGeneration) return;
          applyCanonical(page.messages);
          if (page.nextAfterSeq === null) break;
          cursor = page.nextAfterSeq;
        }
      }
    };
    reconciliation.current = run()
      .catch((error) => {
        if (generation.current === runGeneration) {
          setHistoryError(error instanceof Error ? error.message : "Failed to load history");
        }
      })
      .finally(() => {
        reconciliation.current = null;
        if (requestedAfter.current !== null && generation.current === runGeneration) {
          void reconcile(requestedAfter.current);
        }
      });
    return reconciliation.current;
  }, [room, messaging, applyCanonical]);

  const subscriptionPath = room ? `/rooms/${room.roomId}/${room.deliveryKey}` : null;
  useEffect(() => {
    if (!subscriptionPath || !liveChannels.includes(subscriptionPath)) return;
    void reconcile(lastSequence(messagesRef.current));
  }, [subscriptionPath, liveChannels, connectionRevision, historyNonce, reconcile]);

  useEffect(() => {
    if (!room) return;
    const interval = setInterval(() => {
      void messaging.getRoom(room.roomId).then((detail) => {
        setRoom((current) => current?.roomId === detail.roomId ? detail : current);
      }).catch((error) => {
        if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
          messagesRef.current = [];
          setMessages([]);
          setRoom(null);
        }
      });
    }, 60_000);
    return () => clearInterval(interval);
  }, [room?.roomId, messaging]);

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
      void reconcile(lastSequence(messagesRef.current));
    }
  }, [reconcile]);

  useEffect(() => onEvent((event) => {
    if (!room) return;
    if (event.type === "room.access.removed" && event.payload.roomId === room.roomId) {
      messagesRef.current = [];
      setMessages([]);
      setRoom(null);
      return;
    }
    if (event.type === "room.subscription.rotated" && event.payload.roomId === room.roomId) {
      setRoom((current) => current ? {
        ...current,
        deliveryKey: event.payload.deliveryKey,
        roomRevision: event.payload.roomRevision,
      } : current);
      return;
    }
    if (event.type === "room.metadata.changed" && event.payload.roomId === room.roomId) {
      void messaging.getRoom(room.roomId).then(setRoom).catch(() => {});
      return;
    }
    if (event.type === "room.message.created" && event.payload.roomId === room.roomId) {
      const previous = lastSequence(messagesRef.current);
      applyCanonical([event.payload]);
      if (event.payload.messageSeq > previous + 1) void reconcile(previous);
      return;
    }
    if (
      event.type === "room.run.frame" &&
      event.payload.roomId === room.roomId &&
      event.payload.agentId === agentId
    ) {
      handleFrame(event.payload.frame, event.payload.runId);
    }
  }), [room, agentId, onEvent, messaging, applyCanonical, reconcile, handleFrame]);

  useEffect(() => {
    const sequence = lastSequence(messages);
    if (!room || sequence <= room.lastReadSeq) return;
    void messaging.markRead(room.roomId, { lastReadSeq: sequence }).then((cursor) => {
      setRoom((current) => current ? { ...current, lastReadSeq: cursor.lastReadSeq } : current);
    }).catch(() => {});
  }, [messages, room, messaging]);

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
    if (!text || !room || !agentId || !httpReady) return false;
    let pending = pendingSendRef.current;
    if (!pending || pending.text !== text) {
      pending = { clientMessageId: randomUUID(), text, uncertain: false };
      pendingSendRef.current = pending;
    }
    const localId = `pending-${pending.clientMessageId}`;
    setItems((current) => current.some((item) => item.id === localId)
      ? current
      : [...current, { id: localId, role: "user", text }]);
    if (pending.uncertain) await reconcile(lastSequence(messagesRef.current));
    try {
      const canonical = await messaging.sendMessage(room.roomId, {
        clientMessageId: pending.clientMessageId,
        text,
        mentions: [],
        replyToMessageId: null,
        threadRootMessageId: null,
      });
      pendingSendRef.current = null;
      setItems((current) => current.filter((item) => item.id !== localId));
      applyCanonical([canonical]);
      if (canonical.runId) activeRunIdRef.current = canonical.runId;
      setBusy(true);
      return true;
    } catch (error) {
      pending.uncertain = !(error instanceof ApiError);
      setItems((current) => [
        ...current.filter((item) => item.id !== localId),
        {
          id: randomUUID(),
          role: "error",
          text: error instanceof Error ? error.message : "Send failed",
        },
      ]);
      return false;
    }
  }, [room, agentId, httpReady, messaging, reconcile, applyCanonical]);

  const send = useCallback(async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || uploading || !agentId || !room) return;
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
  }, [input, attachments, uploading, agentId, room, dispatchText]);

  const stopRun = useCallback(async () => {
    const runId = activeRunIdRef.current;
    if (!runId || !httpReady) return;
    try {
      await messaging.abortRun(runId);
    } catch (error) {
      setItems((current) => [...current, {
        id: randomUUID(),
        role: "error",
        text: error instanceof Error ? error.message : "Stop failed",
      }]);
    }
  }, [httpReady, messaging]);

  const agentName = name || agentId || "Agent";
  const allItems = useMemo(
    () => [...messages.map((message) => messageItem(message, userId ?? null)), ...items],
    [messages, userId, items],
  );
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
        ListHeaderComponent={historyError ? (
          <View style={[styles.errorCard, styles.historyErrorCard]}>
            <Text style={styles.errorText}>Couldn’t load history — {historyError}</Text>
            <Pressable
              onPress={() => setHistoryNonce((nonce) => nonce + 1)}
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
