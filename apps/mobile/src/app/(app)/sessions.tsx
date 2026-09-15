import { useAuth } from "@clerk/clerk-expo";
import { sessionLabel, sessionStatus, useWorkspaceSessions, workspaceSections, type LocalSession } from "@lightbulb/shared";
import { router, Stack, useIsFocused } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Pressable, SectionList, StyleSheet, Text, View } from "react-native";

import { SessionStatusDot } from "../../components/session/SessionStatusDot";
import { colors, fonts, radii, spacing } from "../../config/theme";
import { memberName } from "../../lib/localSessions";
import { relativeTime } from "../../lib/relativeTime";
import { useMobileRooms } from "../../lib/rooms";
import { fetchMembers, useTeamsConfig, type CompanyMember } from "../../lib/teamsData";

/**
 * Every live shared session in the workspace, grouped by person (yours
 * first) — the phone's twin of the web sidebar's session list. Tapping a
 * session resolves the same room web does (rooms.sessionContextRoom) and
 * pushes the existing session screen.
 */
export default function SharedSessionsScreen() {
  const { userId } = useAuth();
  const focused = useIsFocused();
  // visible: false — this list belongs to no single room, so it marks nothing read.
  const rooms = useMobileRooms({ subscribeToRoom: false, autoSelect: false, visible: false });
  const { sessions, error, loading, refresh } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused });
  // The spinner is for a pull only: `loading` is also true for every 30 s poll and hint.
  const [pulling, setPulling] = useState(false);
  const members = useMemo(() => rooms.rooms.flatMap((room) => room.members ?? []), [rooms.rooms]);
  // This list is workspace-wide, so a live session can belong to someone the
  // viewer shares no room with -- name from the org roster first (the same
  // GET /teams/members NewMessageSheet uses), falling back to room members.
  // No ref-pin needed here (unlike NewMessageSheet's own per-open fetch):
  // useTeamsConfig's adapter is already identity-stable (its own useMemo has
  // an empty deps array), so plain [config] never re-fires this effect.
  const config = useTeamsConfig();
  const [roster, setRoster] = useState<CompanyMember[]>([]);
  useEffect(() => {
    let alive = true;
    void fetchMembers(config).then((list) => { if (alive) setRoster(list); });
    return () => { alive = false; };
  }, [config]);
  const sections = useMemo(
    () => workspaceSections(sessions, userId, (human) => {
      const member = roster.find((m) => m.userId === human);
      return member ? (member.name || member.email || "Workspace member") : memberName(members, human);
    }),
    [sessions, userId, members, roster],
  );
  const [opening, setOpening] = useState<string | null>(null);
  // Guards against a second row's tap starting a second open while the first
  // is still resolving — a ref (not state) so the check is synchronous even
  // mid-await, and both completions' `finally` clear only their own attempt.
  const openingRef = useRef(false);
  const open = async (session: LocalSession) => {
    if (openingRef.current) return;
    openingRef.current = true;
    setOpening(session.sessionId);
    try {
      const room = rooms.sessionContextRoom(session, userId);
      // A rejected DM resolve sets rooms.selectRoomError, rendered below (this
      // screen — unlike web's global banner — is its own error surface);
      // swallow the throw so the fire-and-forget onPress doesn't reject unhandled.
      const roomId = room instanceof Promise ? await room.catch(() => null) : room;
      if (roomId) router.push(`/(app)/room/${roomId}/session/${session.sessionId}`);
    } finally {
      openingRef.current = false;
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
      {rooms.selectRoomError && (
        <View style={styles.errorCard}>
          <Text accessibilityRole="alert" style={styles.errorText}>{rooms.selectRoomError}</Text>
        </View>
      )}
      <SectionList
        testID="shared-sessions"
        sections={sections}
        keyExtractor={(row) => row.sessionId}
        contentContainerStyle={[styles.content, sections.length === 0 && styles.contentEmpty]}
        refreshing={pulling}
        onRefresh={() => {
          setPulling(true);
          void refresh().finally(() => setPulling(false));
        }}
        renderSectionHeader={({ section }) => <Text accessibilityRole="header" style={styles.sectionTitle}>{section.title}</Text>}
        renderItem={({ item }) => (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Open ${sessionLabel(item)}`}
            accessibilityState={{ busy: opening === item.sessionId }}
            disabled={opening !== null}
            style={({ pressed }) => [styles.row, pressed && styles.rowPressed, opening === item.sessionId && styles.rowOpening]}
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
  rowOpening: { opacity: 0.6 },
  rowText: { flex: 1 },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  rowMeta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  empty: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  errorCard: { padding: spacing.lg, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18, color: colors.forest },
});
