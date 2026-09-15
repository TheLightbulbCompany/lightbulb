import { useAuth } from "@clerk/clerk-expo";
import { sessionLabel, sessionStatus, useWorkspaceSessions, type LocalSession } from "@lightbulb/shared";
import { router, Stack, useFocusEffect } from "expo-router";
import { useCallback, useMemo, useState } from "react";
import { Pressable, SectionList, StyleSheet, Text, View } from "react-native";

import { SessionStatusDot } from "../../components/session/SessionStatusDot";
import { colors, fonts, radii, spacing } from "../../config/theme";
import { memberName, workspaceSections } from "../../lib/localSessions";
import { relativeTime } from "../../lib/relativeTime";
import { useMobileRooms } from "../../lib/rooms";

/**
 * Every live shared session in the workspace, grouped by person (yours
 * first) — the phone's twin of the web sidebar's session list. Tapping a
 * session resolves the same room web does (your own active/first room, or
 * the sharer's DM) and pushes the existing session screen.
 */
export default function SharedSessionsScreen() {
  const { userId } = useAuth();
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  // visible: false — this list belongs to no single room, so it marks nothing read.
  const rooms = useMobileRooms({ subscribeToRoom: false, autoSelect: false, visible: false });
  const { sessions, error, loading, refresh } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused });
  const members = useMemo(() => rooms.rooms.flatMap((room) => room.members ?? []), [rooms.rooms]);
  const sections = useMemo(
    () => workspaceSections(sessions, userId, (human) => memberName(members, human)),
    [sessions, userId, members],
  );
  const [opening, setOpening] = useState<string | null>(null);
  const open = async (session: LocalSession) => {
    setOpening(session.sessionId);
    try {
      const roomId = session.humanUserId === userId
        ? rooms.activeRoomId ?? rooms.rooms[0]?.roomId ?? null
        // A failed resolve just means the tap does nothing; selectRoomError already covers it elsewhere.
        : await rooms.selectDm({ kind: "human", id: session.humanUserId }, { activate: false }).catch(() => null);
      if (roomId) router.push(`/(app)/room/${roomId}/session/${session.sessionId}`);
    } finally {
      setOpening(null);
    }
  };

  return (
    <View style={styles.container}>
      <Stack.Screen options={{ title: "Shared sessions" }} />
      {error && (
        <View style={styles.errorCard}>
          <Text accessibilityRole="alert" style={styles.errorText}>{error}</Text>
          <Pressable onPress={() => void refresh()} accessibilityRole="button" accessibilityLabel="Try again">
            <Text style={styles.link}>Try again</Text>
          </Pressable>
        </View>
      )}
      <SectionList
        sections={sections}
        keyExtractor={(row) => row.sessionId}
        contentContainerStyle={[styles.content, sections.length === 0 && styles.contentEmpty]}
        refreshing={loading}
        onRefresh={() => void refresh()}
        renderSectionHeader={({ section }) => <Text style={styles.sectionTitle}>{section.title}</Text>}
        renderItem={({ item }) => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Open ${sessionLabel(item)}`}
            disabled={opening === item.sessionId}
            style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
            onPress={() => void open(item)}
          >
            <SessionStatusDot status={item.status} />
            <View style={styles.rowText}>
              <Text style={styles.rowLabel} numberOfLines={1}>{sessionLabel(item)}</Text>
              <Text style={styles.rowMeta} numberOfLines={1}>
                {sessionStatus[item.status]}
                {item.lastEventAt ? ` · ${relativeTime(Date.parse(item.lastEventAt))}` : ""}
              </Text>
            </View>
          </Pressable>
        )}
        ListEmptyComponent={
          loading ? (
            <Text style={styles.empty}>Loading shared sessions…</Text>
          ) : error ? null : (
            <Text style={styles.empty}>No one is sharing a terminal right now.</Text>
          )
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  content: { paddingHorizontal: spacing.lg, paddingTop: spacing.sm, paddingBottom: spacing.xl },
  contentEmpty: { flexGrow: 1, justifyContent: "center", alignItems: "center" },
  sectionTitle: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20, color: colors.ink, marginTop: spacing.lg, marginBottom: spacing.sm },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.sm, paddingVertical: spacing.sm, borderRadius: radii.sm },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowText: { flex: 1 },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  rowMeta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  empty: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  errorCard: { padding: spacing.lg, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18, color: colors.forest },
});
