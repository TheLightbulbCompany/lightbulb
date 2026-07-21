import Feather from "@expo/vector-icons/Feather";
import { useCallback, useEffect, useState } from "react";
import {
  ActionSheetIOS,
  ActivityIndicator,
  Alert,
  FlatList,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";
import {
  projectSessionRows,
  relativeTime,
  SESSIONS_LIST_PARAMS,
  type AgentSession,
} from "../../lib/agentSessions";
import { useGateway } from "../../providers/gateway";

interface SessionsSheetProps {
  visible: boolean;
  onClose: () => void;
  agentId: string;
  /** Clerk user id; null while Clerk is still loading (list waits on it). */
  userId: string | null;
  activeSessionId: string | null;
  /** Switch the chat screen to a session (null == General). */
  onSelect: (sessionId: string | null, label: string | null) => void;
  /** Mint + activate a brand-new session (screen owns the UUID). */
  onNewSession: () => void;
}

/**
 * Bottom-sheet session switcher (sessions contract §3, design spec §5
 * adapted): slide-up cream card with a drag-handle bar, a "New session"
 * action, and one row per session — General first, then named sessions
 * newest-first. Long-press a named session to delete it.
 */
export function SessionsSheet({
  visible,
  onClose,
  agentId,
  userId,
  activeSessionId,
  onSelect,
  onNewSession,
}: SessionsSheetProps) {
  const gateway = useGateway();
  // null == never loaded (spinner); stale rows stay visible during a
  // refetch (reopen / post-delete revalidation).
  const [sessions, setSessions] = useState<AgentSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  // A different agent means a different session namespace — the sheet is
  // reused across route params, so drop the previous agent's rows (back to
  // the loading state) and let the next open refetch.
  useEffect(() => {
    setSessions(null);
    setError(null);
  }, [agentId]);

  useEffect(() => {
    if (!visible || !agentId || !userId) return;
    let cancelled = false;
    setError(null);
    gateway
      .sendReq("sessions.list", { ...SESSIONS_LIST_PARAMS })
      .then((payload) => {
        if (cancelled) return;
        setSessions(projectSessionRows(payload, agentId, userId));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load sessions");
      });
    return () => {
      cancelled = true;
    };
  }, [visible, agentId, userId, gateway, nonce]);

  const deleteSession = useCallback(
    (session: AgentSession) => {
      // sessions.delete drops the session entry; deleteTranscript archives
      // its transcript with it (contract §3). Deleting the active session
      // falls back to General.
      gateway
        .sendReq("sessions.delete", { key: session.key, deleteTranscript: true })
        .then(() => {
          if (session.sessionId === activeSessionId) onSelect(null, null);
          setNonce((n) => n + 1);
        })
        .catch((err: unknown) => {
          setError(err instanceof Error ? err.message : "Failed to delete session");
        });
    },
    [gateway, activeSessionId, onSelect],
  );

  const confirmDelete = useCallback(
    (session: AgentSession) => {
      if (session.sessionId === null) return; // General is not deletable
      if (Platform.OS === "ios") {
        ActionSheetIOS.showActionSheetWithOptions(
          {
            title: session.label,
            options: ["Delete session", "Cancel"],
            destructiveButtonIndex: 0,
            cancelButtonIndex: 1,
          },
          (index) => {
            if (index === 0) deleteSession(session);
          },
        );
      } else {
        Alert.alert(session.label, "Delete this session?", [
          { text: "Cancel", style: "cancel" },
          { text: "Delete", style: "destructive", onPress: () => deleteSession(session) },
        ]);
      }
    },
    [deleteSession],
  );

  return (
    <BottomSheet
      visible={visible}
      onClose={onClose}
      closeLabel="Close sessions"
      maxHeight="70%"
    >
      <View style={styles.card}>
        <View style={styles.handle} />
        <View style={styles.header}>
          <Text style={styles.overline}>SESSIONS</Text>
          <Pressable
            style={({ pressed }) => [styles.newAction, pressed && styles.pressedRow]}
            onPress={onNewSession}
            accessibilityRole="button"
            accessibilityLabel="New session"
          >
            <Feather name="plus" size={14} color={colors.forest} />
            <Text style={styles.newActionText}>New session</Text>
          </Pressable>
        </View>
        {sessions === null && !error ? (
          <View style={styles.stateWell}>
            <ActivityIndicator color={colors.forest} />
          </View>
        ) : sessions === null ? (
          // Initial load failed — nothing to show but the error + retry.
          <View style={styles.stateWell}>
            <Text style={styles.errorText}>{error}</Text>
            <Pressable
              onPress={() => setNonce((n) => n + 1)}
              accessibilityRole="button"
              accessibilityLabel="Retry loading sessions"
            >
              <Text style={styles.retryText}>Retry</Text>
            </Pressable>
          </View>
        ) : (
          <>
            {error ? (
              // A delete/refetch failure with rows already loaded must not
              // blank the list — slim inline message above it instead.
              <View style={styles.inlineError}>
                <Text style={styles.errorText}>{error}</Text>
              </View>
            ) : null}
            <FlatList
              data={sessions ?? []}
              keyExtractor={(s) => s.sessionId ?? "general"}
              contentContainerStyle={styles.list}
              renderItem={({ item }) => {
                const active = item.sessionId === activeSessionId;
                return (
                  <Pressable
                    style={({ pressed }) => [
                      styles.row,
                      active && styles.rowActive,
                      pressed && !active && styles.pressedRow,
                    ]}
                    onPress={() => {
                      onSelect(item.sessionId, item.sessionId === null ? null : item.label);
                      onClose();
                    }}
                    onLongPress={() => confirmDelete(item)}
                    accessibilityRole="button"
                    accessibilityLabel={item.label}
                  >
                    <View style={styles.rowTop}>
                      <Text style={styles.rowLabel} numberOfLines={1}>
                        {item.label}
                      </Text>
                      {item.updatedAt ? (
                        <Text style={styles.rowTime}>{relativeTime(item.updatedAt)}</Text>
                      ) : null}
                    </View>
                    {item.preview ? (
                      <Text style={styles.rowPreview} numberOfLines={1}>
                        {item.preview}
                      </Text>
                    ) : null}
                  </Pressable>
                );
              }}
            />
          </>
        )}
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingBottom: spacing.xl,
  },
  handle: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.edge,
    marginTop: spacing.sm,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.lg,
    paddingBottom: spacing.sm,
  },
  // Mono section label (§5): 10px, tracked .14em, uppercase content.
  overline: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
  },
  newAction: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.xs,
    borderRadius: radii.sm,
  },
  newActionText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  list: { paddingHorizontal: spacing.sm, paddingBottom: spacing.sm },
  row: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radii.md,
    gap: 2,
  },
  rowActive: { backgroundColor: colors.sessionActive },
  pressedRow: { backgroundColor: colors.rowPressed },
  rowTop: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  rowLabel: {
    flexShrink: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  rowTime: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 14,
    color: colors.mute,
  },
  rowPreview: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  stateWell: {
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.xl,
    paddingHorizontal: spacing.lg,
  },
  // Slim inline failure strip above a still-rendered list (delete/refetch
  // errors) — same error palette as the chat error card.
  inlineError: {
    marginHorizontal: spacing.lg,
    marginBottom: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.sm,
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
    textAlign: "center",
  },
  retryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
});
