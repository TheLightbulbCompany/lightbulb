import { useAuth } from "@clerk/clerk-expo";
import { sessionLabel, sessionStatus, sessionTitle, useWorkspaceSessions, workspaceSections } from "@lightbulb/shared";
import { router, Stack, useIsFocused } from "expo-router";
import { useEffect, useMemo, useState } from "react";
import { AppState, Pressable, SectionList, StyleSheet, Text, View } from "react-native";

import { SessionStatusDot } from "../../components/session/SessionStatusDot";
import { SessionToolIcon } from "../../components/session/SessionToolIcon";
import { colors, fonts, radii, spacing } from "../../config/theme";
import { memberName } from "../../lib/localSessions";
import { relativeTime } from "../../lib/relativeTime";
import { useMobileRooms } from "../../lib/rooms";
import { fetchMembers, useTeamsConfig, type CompanyMember } from "../../lib/teamsData";

/**
 * Every live shared session in the workspace, grouped by person (yours
 * first) — the phone's twin of the web sidebar's session list. Tapping a
 * session pushes its workspace-scoped session screen; no room is involved.
 */
export default function SharedSessionsScreen() {
  const { userId } = useAuth();
  const focused = useIsFocused();
  // visible: false — this list belongs to no single room, so it marks nothing read.
  const rooms = useMobileRooms({ subscribeToRoom: false, autoSelect: false, visible: false });
  const { sessions, error, loading, refresh } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused });
  // Foregrounding with this screen on top changes neither focus nor (if the
  // socket survived suspension) connectionRevision, so neither refresh path
  // fires. Re-read explicitly: returning to the phone is exactly when the
  // list must not show a pre-background snapshot.
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => {
      if (state === "active" && focused) void refresh();
    });
    return () => subscription.remove();
  }, [focused, refresh]);
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
            style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
            onPress={() => router.push(`/(app)/session/${item.sessionId}`)}
          >
            <View>
              <SessionToolIcon provider={item.provider} />
              <View style={styles.rowDot}><SessionStatusDot status={item.status} /></View>
            </View>
            <View style={styles.rowText}>
              <Text style={styles.rowLabel} numberOfLines={1}>{sessionTitle(item)}</Text>
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
  rowDot: { position: "absolute", right: -3, bottom: -3, borderRadius: 6, borderWidth: 2, borderColor: colors.cream },
  rowText: { flex: 1 },
  rowLabel: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  rowMeta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  empty: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  errorCard: { padding: spacing.lg, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18, color: colors.forest },
});
