import Feather from "@expo/vector-icons/Feather";
import { sessionLabel, sessionStatus, useRoomResources, type LocalSession } from "@lightbulb/shared";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, SectionList, StyleSheet, Text, View } from "react-native";

import { RoomBlockedState } from "../../../../components/room/RoomBlockedState";
import { SessionStatusDot } from "../../../../components/session/SessionStatusDot";
import { colors, fonts, radii, spacing } from "../../../../config/theme";
import { deviceSections, memberName, type DeviceRow } from "../../../../lib/localSessions";
import { useMobileRooms } from "../../../../lib/rooms";

/**
 * Devices & sessions for one room: the Macs and shared Claude / Codex sessions
 * its members expose. The web room tab's list, pushed over the conversation.
 */
export default function DevicesScreen() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  // visible: false — this is not the room; it marks nothing read (the thread screen's rule).
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false });
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);
  const resources = useRoomResources(roomId ?? "", rooms.resourceSource, { enabled: focused && !!roomId });
  const members = rooms.activeRoom?.members;
  const sections = resources.data ? deviceSections(resources.data, (human) => memberName(members, human)) : [];

  const headerOptions = {
    // The back button is the (app) Stack's own: this is a pushed screen.
    title: "Devices & sessions",
  };

  if (rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <RoomBlockedState
          state={rooms.accessRemoved ? "access-removed" : "unavailable"}
          onRetry={rooms.accessRemoved ? undefined : rooms.refreshRoom}
        />
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Stack.Screen options={headerOptions} />
      {resources.error && (
        <View style={styles.errorCard}>
          <Text accessibilityRole="alert" style={styles.errorText}>{resources.error}</Text>
          {resources.data && <Text style={styles.meta}>Showing the last successful snapshot; availability may be stale.</Text>}
          <Pressable onPress={() => void resources.refresh()} accessibilityRole="button" accessibilityLabel="Try again">
            <Text style={styles.link}>Try again</Text>
          </Pressable>
        </View>
      )}
      <SectionList
        sections={sections}
        keyExtractor={(row) => row.key}
        contentContainerStyle={[styles.list, sections.length === 0 && styles.listEmpty]}
        stickySectionHeadersEnabled={false}
        renderSectionHeader={({ section }) => <Text style={styles.owner}>{section.title}</Text>}
        renderItem={({ item }) => (
          <DeviceCard row={item} onOpen={(session) => router.push(`/(app)/room/${roomId}/session/${session.sessionId}`)} />
        )}
        ListEmptyComponent={
          resources.loading ? (
            <Text style={styles.meta}>Loading devices and sessions…</Text>
          ) : resources.error ? null : (
            <Text style={styles.meta}>No paired devices or shared sessions in this conversation.</Text>
          )
        }
        ListFooterComponent={
          sections.length > 0 ? (
            <Text style={styles.footer}>
              Paired endpoints are listed independently of session sharing. Pairing does not confirm they are online or available to run work.
            </Text>
          ) : null
        }
      />
    </View>
  );
}

function DeviceCard({ row, onOpen }: { row: DeviceRow; onOpen: (session: LocalSession) => void }) {
  if (row.kind === "cli") {
    return (
      <View style={styles.card}>
        <View style={styles.cardHeader}>
          <Feather name="terminal" size={16} color={colors.inkSoft} />
          <Text style={styles.cardTitle}>Paired CLI endpoint · {row.deviceId.slice(0, 8)}</Text>
        </View>
        <Text style={styles.meta}>Availability unknown · no Mac association verified</Text>
      </View>
    );
  }
  return (
    <View style={styles.card}>
      <View style={styles.cardHeader}>
        <Feather name="monitor" size={16} color={colors.inkSoft} />
        <Text style={styles.cardTitle}>Mac · {row.deviceId.slice(0, 8)}</Text>
      </View>
      {row.pairedLabel && <Text style={styles.meta}>{row.pairedLabel}</Text>}
      {row.sessions.map((session) => (
        <Pressable
          key={session.sessionId}
          onPress={() => onOpen(session)}
          accessibilityRole="button"
          accessibilityLabel={`Open ${sessionLabel(session)}`}
          style={({ pressed }) => [styles.session, pressed && styles.sessionPressed]}
        >
          <SessionStatusDot status={session.status} />
          <View style={styles.sessionBody}>
            <Text style={styles.sessionTitle} numberOfLines={1}>{sessionLabel(session)}</Text>
            <Text style={styles.sessionMeta} numberOfLines={1}>
              {sessionStatus[session.status]}{session.cwdName ? ` · ~/${session.cwdName}` : ""}
            </Text>
          </View>
          <Feather name="chevron-right" size={16} color={colors.mute} />
        </Pressable>
      ))}
      {row.sessions.length === 0 && <Text style={styles.meta}>No shared local sessions.</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  list: { paddingHorizontal: spacing.lg, paddingTop: spacing.sm, paddingBottom: spacing.xl },
  listEmpty: { flexGrow: 1, justifyContent: "center", alignItems: "center" },
  owner: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20, color: colors.ink, marginTop: spacing.lg, marginBottom: spacing.sm },
  card: {
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.md,
    padding: spacing.md,
    marginBottom: spacing.sm,
    gap: spacing.xs,
  },
  cardHeader: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  cardTitle: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  session: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
  },
  sessionPressed: { backgroundColor: colors.rowPressed },
  sessionBody: { flex: 1 },
  sessionTitle: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  sessionMeta: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 16, color: colors.mute },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  footer: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute, marginTop: spacing.lg },
  errorCard: { padding: spacing.lg, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18, color: colors.forest },
});
