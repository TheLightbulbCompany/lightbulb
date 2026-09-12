import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import {
  buildTimeline,
  frameText,
  roomDisplayName,
  runView,
  type CanonicalRoomMessage,
  type RoomRunView,
  type TimelineItem,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Animated,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  type NativeScrollEvent,
  type NativeSyntheticEvent,
} from "react-native";

import { AgentHead } from "../../../../components/chat/AgentHead";
import { EmptyState } from "../../../../components/chat/EmptyState";
import { MessageMarkdown } from "../../../../components/chat/MessageMarkdown";
import { TimelineSkeleton } from "../../../../components/chat/TimelineSkeleton";
import { ToolPill } from "../../../../components/chat/ToolPill";
import { usePulse } from "../../../../components/chat/usePulse";
import { MessageGroup } from "../../../../components/room/MessageGroup";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import { useMobileRooms } from "../../../../lib/rooms";

/** Distance from the bottom, in px, still counted as "at the bottom". */
const BOTTOM_SLACK = 80;

function ReconnectingBanner() {
  const pulse = usePulse(true, 0.3, 700);
  return (
    <View style={styles.banner}>
      <Animated.View style={[styles.bannerDot, { opacity: pulse }]} />
      <Text style={styles.bannerText}>Reconnecting live updates. You can keep sending.</Text>
    </View>
  );
}

/** A live (or just-finished, not-yet-persisted) agent run. */
function RunBlock({ run, name }: { run: RoomRunView; name: string }) {
  return (
    <View style={styles.run}>
      <AgentHead name={name} active={run.state === "running"} />
      {run.tools.map((tool) => (
        <View key={tool.id} style={styles.pillGap}>
          <ToolPill
            tool={tool.name}
            status={tool.status}
            args={
              tool.args && typeof tool.args === "object" && !Array.isArray(tool.args)
                ? (tool.args as Record<string, unknown>)
                : undefined
            }
            result={frameText(tool.result) ?? tool.progressText}
          />
        </View>
      ))}
      {run.text.trim() ? <MessageMarkdown text={run.text} /> : null}
    </View>
  );
}

export default function ConversationRoute() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  // Remount on a room change: the shared machine keys its history, runs and
  // read cursor off the mounted instance.
  return <Conversation key={roomId ?? "none"} />;
}

function Conversation() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  const { userId } = useAuth();
  // The SCREEN's own focus. expo-router blurs a pushed screen (a thread) without
  // backgrounding the app, and read marking is "on screen AND foregrounded".
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: focused });

  // selectRoom's identity changes with every message (it closes over
  // markVisibleRead), and each call bumps the hook's selection-cancellation
  // counter — so fire it on roomId only, through a ref.
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);

  // Pinned at open so the "New messages" line does not walk down the list as
  // messages are marked read (mirrors MessagingWorkspaceContext's readSeqAtOpen).
  const readSeqAtOpen = useRef<number | null>(null);
  if (rooms.activeRoom && readSeqAtOpen.current === null) {
    readSeqAtOpen.current = rooms.activeRoom.lastReadSeq;
  }
  const readSeq = readSeqAtOpen.current;

  // Thread replies live on the thread screen, not in the room's own timeline
  // (web RoomPanel does the same split), but they still feed the reply counts.
  const mainMessages = useMemo(
    () => rooms.messages.filter((message) => message.threadRootMessageId === null),
    [rooms.messages],
  );
  const items = useMemo(() => buildTimeline(mainMessages, { readSeq }), [mainMessages, readSeq]);
  const replyCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const message of rooms.messages) {
      if (message.threadRootMessageId) {
        counts.set(message.threadRootMessageId, (counts.get(message.threadRootMessageId) ?? 0) + 1);
      }
    }
    return counts;
  }, [rooms.messages]);
  const runs = useMemo(
    () => rooms.runs.filter((run) => !run.threadRootMessageId).map(runView),
    [rooms.runs],
  );

  const members = rooms.activeRoom?.members;
  const viewer = members?.find((member) => member.kind === "human" && member.id === userId);
  const others = (members ?? []).filter(
    (member) => !(member.kind === "human" && member.id === userId),
  );
  const isChannel = rooms.activeRoom?.kind === "channel";
  // ponytail: Phase A's RoomSummary carries no private flag, so every channel
  // gets `#`; swap in a lock glyph once the backend ships one.
  const title = `${isChannel ? "#" : ""}${roomDisplayName(rooms.activeRoom, others)}`;

  // The server answers before the optimistic row would otherwise be visible on
  // a fast network; on a slow one this is the difference between "sent" and
  // "nothing happened". The hook keeps the row (with its sendState) on failure.
  const pending = useMemo<CanonicalRoomMessage[]>(
    () =>
      rooms.optimisticMessages
        .filter((message) => message.threadRootMessageId === null)
        .map((message) => ({
          messageId: message.clientMessageId,
          roomId: message.roomId,
          messageSeq: 0,
          roomRevision: 0,
          author: {
            kind: "human" as const,
            id: userId ?? "",
            displayName: viewer?.displayName ?? "You",
          },
          text: message.text,
          mentions: message.mentions,
          replyToMessageId: message.replyToMessageId,
          threadRootMessageId: message.threadRootMessageId,
          createdAt: message.createdAt,
          runId: null,
        })),
    [rooms.optimisticMessages, userId, viewer?.displayName],
  );

  const listRef = useRef<FlatList<TimelineItem>>(null);
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    const next = contentSize.height - layoutMeasurement.height - contentOffset.y < BOTTOM_SLACK;
    atBottomRef.current = next;
    setAtBottom(next);
  }, []);

  const [input, setInput] = useState("");
  const [inputFocused, setInputFocused] = useState(false);
  const [sending, setSending] = useState(false);
  const { sendMessage, activeRoomId, httpReady } = rooms;
  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || sending || !activeRoomId || !httpReady) return;
    setSending(true);
    setInput("");
    try {
      await sendMessage({
        clientMessageId: randomUUID(),
        text,
        mentions: [],
        replyToMessageId: null,
        threadRootMessageId: null,
      });
    } catch {
      // The hook keeps the optimistic row and its sendState; Task 10's composer
      // owns the retry affordance.
    } finally {
      setSending(false);
    }
  }, [input, sending, activeRoomId, httpReady, sendMessage]);

  const runningRun = runs.find((run) => run.state === "running");
  const agentName = useCallback(
    (agentId: string) =>
      members?.find((member) => member.kind === "agent" && member.id === agentId)?.displayName ??
      "Agent",
    [members],
  );

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={90}
    >
      <Stack.Screen
        options={{
          title,
          headerTitle: () => (
            <View style={styles.headerTitle}>
              <Text style={styles.headerName} numberOfLines={1}>
                {title}
              </Text>
              {isChannel && members ? (
                <Text style={styles.headerMeta}>
                  {members.length} {members.length === 1 ? "member" : "members"}
                </Text>
              ) : null}
            </View>
          ),
        }}
      />
      <FlatList
        ref={listRef}
        testID="room-timeline"
        data={items}
        keyExtractor={(item) => item.key}
        contentContainerStyle={[styles.list, items.length === 0 && styles.listEmpty]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onScroll={onScroll}
        scrollEventThrottle={16}
        onContentSizeChange={() => {
          if (atBottomRef.current) listRef.current?.scrollToEnd({ animated: true });
        }}
        ListHeaderComponent={
          rooms.historyError ? (
            <View style={styles.errorCard}>
              <Text style={styles.errorText}>Couldn’t load history.</Text>
              <Pressable
                onPress={rooms.loadHistory}
                accessibilityRole="button"
                accessibilityLabel="Retry loading history"
              >
                <Text style={styles.retryText}>Retry</Text>
              </Pressable>
            </View>
          ) : null
        }
        ListEmptyComponent={
          rooms.historyLoading ? <TimelineSkeleton /> : <EmptyState agentName={title} />
        }
        ListFooterComponent={
          <View>
            {pending.map((message) => (
              <MessageGroup
                key={message.messageId}
                message={message}
                collapsed={false}
                replyCount={0}
                downloadAttachment={rooms.downloadAttachment}
              />
            ))}
            {runs.map((run) => (
              <RunBlock key={run.runId} run={run} name={agentName(run.agentId)} />
            ))}
          </View>
        }
        renderItem={({ item }) => {
          if (item.kind === "date") {
            return (
              <View style={styles.divider}>
                <View style={styles.rule} />
                <Text style={styles.dividerLabel}>{item.label}</Text>
                <View style={styles.rule} />
              </View>
            );
          }
          if (item.kind === "new") {
            return (
              <View style={styles.divider}>
                <View style={[styles.rule, styles.newRule]} />
                <Text style={styles.newLabel}>New messages</Text>
                <View style={[styles.rule, styles.newRule]} />
              </View>
            );
          }
          return (
            <MessageGroup
              message={item.message}
              collapsed={item.collapsed}
              replyCount={replyCounts.get(item.message.messageId) ?? 0}
              onOpenThread={() =>
                router.push(`/(app)/room/${roomId}/thread/${item.message.messageId}`)
              }
              downloadAttachment={rooms.downloadAttachment}
            />
          );
        }}
      />
      {!atBottom && (
        <Pressable
          style={styles.jump}
          onPress={() => listRef.current?.scrollToEnd({ animated: true })}
          accessibilityRole="button"
          accessibilityLabel="Jump to latest"
        >
          <Feather name="arrow-down" size={16} color={colors.white} />
        </Pressable>
      )}
      <View style={styles.composerOuter}>
        {!rooms.liveConnected && <ReconnectingBanner />}
        <View style={[styles.card, inputFocused && styles.cardFocused]}>
          <View style={styles.inputZone}>
            <TextInput
              style={styles.input}
              placeholder="Message"
              placeholderTextColor={colors.placeholder}
              value={input}
              onChangeText={setInput}
              multiline
              editable={httpReady && !sending}
              onFocus={() => setInputFocused(true)}
              onBlur={() => setInputFocused(false)}
            />
          </View>
          <View style={styles.rail}>
            {/* A room can hold several agents, so a run in flight must never
                block sending — Stop only takes the slot while the draft is
                empty, which is exactly when Send would be disabled anyway. */}
            {runningRun && !input.trim() ? (
              <Pressable
                style={styles.sendButton}
                onPress={() => void rooms.abortRun(runningRun.runId).catch(() => {})}
                accessibilityRole="button"
                accessibilityLabel="Stop"
              >
                <Feather name="square" size={14} color={colors.white} />
              </Pressable>
            ) : (
              <Pressable
                style={[styles.sendButton, (!input.trim() || !httpReady) && styles.sendDisabled]}
                onPress={() => void send()}
                disabled={!input.trim() || sending || !httpReady}
                accessibilityRole="button"
                accessibilityLabel="Send"
              >
                {sending ? (
                  <ActivityIndicator size="small" color={colors.white} />
                ) : (
                  <Feather name="arrow-up" size={16} color={colors.white} />
                )}
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
  list: { paddingHorizontal: spacing.lg, paddingTop: spacing.lg, paddingBottom: spacing.xl },
  listEmpty: { flexGrow: 1 },
  headerTitle: { alignItems: "center" },
  headerName: { fontFamily: fonts.sansSemiBold, fontSize: 17, lineHeight: 22, color: colors.ink },
  headerMeta: { fontFamily: fonts.sans, fontSize: 11, lineHeight: 14, color: colors.mute },
  divider: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    marginTop: spacing.xl,
    marginBottom: spacing.xs,
  },
  rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: colors.edge },
  dividerLabel: type.overline,
  newRule: { backgroundColor: colors.danger },
  newLabel: {
    fontFamily: fonts.sansMedium,
    fontSize: 11,
    lineHeight: 14,
    color: colors.danger,
  },
  run: { marginTop: spacing.lg },
  pillGap: { marginTop: spacing.sm },
  errorCard: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.lg,
    marginBottom: spacing.lg,
    gap: spacing.sm,
  },
  errorText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.errorText },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
  jump: {
    position: "absolute",
    right: spacing.lg,
    bottom: 96,
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  composerOuter: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    backgroundColor: colors.cream,
  },
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
  bannerText: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.bannerText,
  },
});
