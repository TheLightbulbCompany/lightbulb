import { useAuth } from "@clerk/clerk-expo";
import { sessionHistory, sessionLabel, sessionLive, sessionMatches, sessionStatus, sessionTitle, sessionUnread, useWorkspaceSessions, workspaceSections, type LocalSession } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import { router, Stack, useIsFocused, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AppState, Pressable, SectionList, StyleSheet, Text, TextInput, View } from "react-native";

import { SessionStatusDot } from "../../components/session/SessionStatusDot";
import { SessionToolIcon } from "../../components/session/SessionToolIcon";
import { colors, fonts, radii, spacing } from "../../config/theme";
import { markSessionsSeenOnDevice, memberName } from "../../lib/localSessions";
import { relativeTime } from "../../lib/relativeTime";
import { useMobileRooms } from "../../lib/rooms";
import { fetchMembers, useTeamsConfig, type CompanyMember } from "../../lib/teamsData";

/**
 * Every live shared session in the workspace (the desktop is mirroring it
 * right now), grouped by person (yours first) — the phone's twin of the web
 * sidebar's session list — with "All sessions" at the bottom. With `?all=1`
 * it is that list instead: every session, searchable, by day, newest first
 * (offline and ended ones open read-only). Tapping a session pushes its
 * workspace-scoped session screen; no room is involved.
 */
export default function SharedSessionsScreen() {
  const { userId } = useAuth();
  const focused = useIsFocused();
  // visible: false — this list belongs to no single room, so it marks nothing read.
  const rooms = useMobileRooms({ subscribeToRoom: false, autoSelect: false, visible: false });
  const { sessions, error, loading, refresh } = useWorkspaceSessions(rooms.resourceSource, { enabled: focused, includeEnded: true });
  const [query, setQuery] = useState("");
  const all = useLocalSearchParams<{ all?: string }>().all === "1";
  const [guideOpen, setGuideOpen] = useState(false);
  // Recomputed with the list (and on focus, after the session screen advanced it).
  const seen = useMemo(() => (focused ? markSessionsSeenOnDevice(sessions, null) : {}), [sessions, focused]);
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
  const name = useCallback((human: string) => {
    if (human === userId) return "You";
    const member = roster.find((m) => m.userId === human);
    return member ? (member.name || member.email || "Workspace member") : memberName(members, human);
  }, [userId, roster, members]);
  const sections = useMemo(() => {
    if (!all) return workspaceSections(sessions.filter(sessionLive), userId, name);
    return sessionHistory(sessions.filter((s) => sessionMatches(s, query, name(s.humanUserId))))
      .map(({ key, title, data }) => ({ key, title, data }));
  }, [sessions, userId, name, query, all]);
  const liveCount = sessions.filter(sessionLive).length;
  return (
    <View style={styles.container}>
      <Stack.Screen options={{ title: all ? "All sessions" : "Shared sessions" }} />
      {error && (
        <View style={styles.errorCard}>
          <Text accessibilityRole="alert" style={styles.errorText}>{error}</Text>
          <Pressable onPress={() => void refresh()} accessibilityRole="button" accessibilityLabel="Try again">
            <Text style={styles.link}>Try again</Text>
          </Pressable>
        </View>
      )}
      {all && (
        <TextInput
          style={styles.filter}
          value={query}
          onChangeText={setQuery}
          placeholder="Search sessions"
          placeholderTextColor={colors.placeholder}
          accessibilityLabel="Search sessions"
          clearButtonMode="while-editing"
          autoCorrect={false}
        />
      )}
      {!all && !loading && !error && liveCount === 0 && (
        <StartSessionGuide open={guideOpen} onToggle={() => setGuideOpen((v) => !v)} />
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
        renderItem={({ item }) => <SessionRow session={item} unread={sessionUnread(item, seen)} owner={all ? name(item.humanUserId) : undefined} />}
        ListFooterComponent={!all && sessions.length > 0 ? (
          <Pressable accessibilityRole="button" accessibilityLabel={`All sessions, ${sessions.length}`}
            style={({ pressed }) => [styles.allRow, pressed && styles.rowPressed]} onPress={() => router.push("/(app)/sessions?all=1")}>
            <Text style={styles.link}>All sessions</Text>
            <Text style={styles.rowMeta}>{sessions.length}</Text>
          </Pressable>
        ) : null}
        ListEmptyComponent={
          loading && sessions.length === 0 ? (
            <Text style={styles.empty}>Loading shared sessions…</Text>
          ) : error || !all ? null : (
            <Text style={styles.empty}>{query.trim() ? "No sessions match." : "No sessions yet."}</Text>
          )
        }
      />
    </View>
  );
}

function SessionRow({ session, unread, owner }: { session: LocalSession; unread: "unread" | "finished" | null; owner?: string }) {
  const news = unread === "finished" ? ", finished since you looked" : unread ? ", new output" : "";
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Open ${sessionLabel(session)}, ${sessionStatus[session.status]}${news}`}
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
      onPress={() => router.push(`/(app)/session/${session.sessionId}`)}
    >
      <View>
        <SessionToolIcon provider={session.provider} />
        <View style={styles.rowDot}><SessionStatusDot status={session.status} /></View>
      </View>
      <View style={styles.rowText}>
        <Text style={[styles.rowLabel, unread && styles.rowLabelUnread]} numberOfLines={1}>{sessionTitle(session)}</Text>
        <Text style={styles.rowMeta} numberOfLines={1}>
          {owner ? `${owner} · ` : ""}{sessionStatus[session.status]}
          {session.lastEventAt ? ` · ${relativeTime(Date.parse(session.lastEventAt))}` : ""}
        </Text>
      </View>
      {unread && <View testID={`unread-${unread}`} style={[styles.unread, unread === "finished" && styles.unreadFinished]} />}
    </Pressable>
  );
}

/** Nobody is sharing: how to start a session. Sessions run in your own terminal; nothing starts one remotely. */
function StartSessionGuide({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <View style={styles.guide}>
      <Text style={styles.empty}>No one is sharing a session.</Text>
      <Pressable accessibilityRole="button" accessibilityState={{ expanded: open }} onPress={onToggle}>
        <Text style={styles.link}>Start a session</Text>
      </Pressable>
      {open && (
        <View style={styles.guideBody}>
          <Text style={styles.guideText}>1. In the Lightbulb desktop app on your Mac, turn on Settings → Native terminal sessions (once).</Text>
          <Text style={styles.guideText}>2. In a terminal in your project, run one of these:</Text>
          {["claude", "codex"].map((command) => (
            <Pressable key={command} accessibilityRole="button" accessibilityLabel={`Copy ${command}`}
              style={styles.command} onPress={() => void Clipboard.setStringAsync(command)}>
              <Text style={styles.commandText}>{command}</Text>
              <Text style={styles.rowMeta}>Copy</Text>
            </Pressable>
          ))}
          <Text style={styles.guideText}>3. It shows up here. Everyone in this workspace can watch; only you can send to it.</Text>
        </View>
      )}
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
  rowLabelUnread: { fontFamily: fonts.sansSemiBold },
  unread: { width: 8, height: 8, borderRadius: 4, backgroundColor: colors.info },
  unreadFinished: { backgroundColor: colors.runningGreen },
  allRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingVertical: spacing.md, marginTop: spacing.md, borderRadius: radii.sm },
  filter: { marginHorizontal: spacing.lg, marginTop: spacing.sm, paddingHorizontal: spacing.sm, paddingVertical: spacing.xs, borderWidth: 1, borderColor: colors.edge, borderRadius: radii.sm, fontFamily: fonts.sans, fontSize: 15, color: colors.ink },
  guide: { paddingHorizontal: spacing.lg, paddingTop: spacing.lg, gap: spacing.xs },
  guideBody: { gap: spacing.sm, paddingTop: spacing.xs },
  guideText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.inkSoft },
  command: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", paddingHorizontal: spacing.sm, paddingVertical: spacing.xs, borderRadius: radii.sm, backgroundColor: colors.codeBg },
  commandText: { fontFamily: fonts.mono, fontSize: 14, color: colors.ink },
  rowMeta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  empty: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  errorCard: { padding: spacing.lg, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18, color: colors.forest },
});
