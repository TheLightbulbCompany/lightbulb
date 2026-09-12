import { useAuth } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import { agentDisplayName, useAgents, type MemberRef } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { useMobileRooms } from "../../lib/rooms";
import { fetchMembers, useTeamsConfig } from "../../lib/teamsData";
import { useLiveEvents } from "../../providers/live-events";
import { BottomSheet } from "../ui/BottomSheet";
import { Avatar } from "./Avatar";

/** One pickable counterpart — a workspace human or one of the owner's agents. */
interface Candidate extends MemberRef {
  displayName: string;
  emoji?: string;
}

const same = (left: MemberRef, right: MemberRef) =>
  left.kind === right.kind && left.id === right.id;

/**
 * The pencil's sheet: pick people AND agents. One pick opens that
 * counterpart's canonical DM (`selectDm` reuses the existing room rather than
 * making a second one); two or more create a group DM.
 *
 * Supersedes the roster's agent-only `AgentPickerSheet`.
 */
export function NewMessageSheet({
  visible,
  onClose,
  onOpenRoom,
}: {
  visible: boolean;
  onClose: () => void;
  onOpenRoom: (roomId: string) => void;
}) {
  const { userId } = useAuth();
  const live = useLiveEvents();
  // Ref-pinned (the pattern `useTeamsConfig` itself uses for getToken) so the
  // roster fetch keys on OPENING the sheet alone: an adapter that changed
  // identity per render would otherwise re-fire the effect below, whose own
  // setState re-renders — an unbounded `/teams/members` loop.
  const config = useTeamsConfig();
  const configRef = useRef(config);
  configRef.current = config;
  const { agents } = useAgents(live);
  // List-only, like the tab that mounts this: the SWR keys are shared with the
  // screen's own hook, so this costs no extra fetch.
  const { selectDm, createRoom } = useMobileRooms({
    autoSelect: false,
    subscribeToRoom: false,
  });
  const [people, setPeople] = useState<Candidate[]>([]);
  const [picked, setPicked] = useState<MemberRef[]>([]);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Opening is what loads the roster and what clears the last visit's picks —
  // a reopened sheet that remembered them would silently add someone to a
  // conversation you never chose.
  useEffect(() => {
    if (!visible) return;
    setPicked([]);
    setQuery("");
    setError(null);
    let alive = true;
    // ponytail: `fetchMembers` is best-effort and resolves to `[]` on failure,
    // so a members outage reads as a workspace with nobody in it while the
    // agents still list. Give it its own error line if that shows up in use.
    void fetchMembers(configRef.current).then((members) => {
      if (!alive) return;
      setPeople(
        members
          .filter((member) => member.userId !== userId)
          .map((member) => ({
            kind: "human" as const,
            id: member.userId,
            displayName: member.name || member.email || "Workspace member",
          })),
      );
    });
    return () => {
      alive = false;
    };
  }, [userId, visible]);

  const candidates = useMemo<Candidate[]>(() => {
    const all: Candidate[] = [
      ...agents.map((agent) => ({
        kind: "agent" as const,
        id: agent.id,
        displayName: agentDisplayName(agent),
        emoji: agent.identity?.emoji,
      })),
      ...people,
    ];
    const needle = query.trim().toLowerCase();
    return needle
      ? all.filter((candidate) => candidate.displayName.toLowerCase().includes(needle))
      : all;
  }, [agents, people, query]);

  const toggle = useCallback((ref: MemberRef) => {
    setPicked((current) =>
      current.some((entry) => same(entry, ref))
        ? current.filter((entry) => !same(entry, ref))
        : [...current, { kind: ref.kind, id: ref.id }],
    );
  }, []);

  const canStart = picked.length > 0 && !busy;
  const start = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const roomId =
        picked.length === 1
          ? await selectDm(picked[0])
          : (
              await createRoom({
                clientRoomId: randomUUID(),
                kind: "group-dm",
                name: null,
                members: picked.map((ref) => ({
                  kind: ref.kind,
                  id: ref.id,
                  role: "member" as const,
                  listenMode: ref.kind === "agent" ? ("active" as const) : null,
                })),
              })
            ).roomId;
      if (roomId) onOpenRoom(roomId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Conversation could not be opened.");
    } finally {
      setBusy(false);
    }
  }, [createRoom, onOpenRoom, picked, selectDm]);

  return (
    <BottomSheet
      visible={visible}
      onClose={onClose}
      closeLabel="Close new message"
      maxHeight="80%"
    >
      <View style={styles.card}>
        <View style={styles.handle} />
        <Text style={styles.overline}>NEW MESSAGE</Text>
        <View style={styles.searchPill}>
          <Feather name="search" size={14} color={colors.placeholder} />
          <TextInput
            style={styles.searchInput}
            placeholder="Search people and agents"
            placeholderTextColor={colors.placeholder}
            value={query}
            onChangeText={setQuery}
            autoCorrect={false}
            autoCapitalize="none"
          />
        </View>
        {error ? <Text style={styles.error}>{error}</Text> : null}
        <FlatList
          data={candidates}
          keyExtractor={(candidate) => `${candidate.kind}:${candidate.id}`}
          contentContainerStyle={styles.list}
          keyboardShouldPersistTaps="handled"
          ListEmptyComponent={
            <Text style={styles.dim}>
              {query.trim() ? "Nobody matches that." : "No one to message yet."}
            </Text>
          }
          renderItem={({ item }) => {
            const chosen = picked.some((entry) => same(entry, item));
            return (
              <Pressable
                onPress={() => toggle(item)}
                accessibilityRole="button"
                accessibilityState={{ selected: chosen }}
                style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
              >
                <Avatar name={item.displayName} kind={item.kind} emoji={item.emoji} />
                <Text style={styles.name} numberOfLines={1}>
                  {item.displayName}
                </Text>
                {chosen ? <Feather name="check" size={16} color={colors.forest} /> : null}
              </Pressable>
            );
          }}
        />
        <Pressable
          onPress={() => void start()}
          disabled={!canStart}
          accessibilityRole="button"
          accessibilityLabel="Start conversation"
          accessibilityState={{ disabled: !canStart }}
          style={({ pressed }) => [
            styles.action,
            !canStart && styles.actionDisabled,
            pressed && styles.actionPressed,
          ]}
        >
          <Text style={styles.actionLabel}>
            {picked.length > 1 ? "Create group" : "Message"}
          </Text>
        </Pressable>
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
  overline: {
    ...type.overline,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.lg,
    paddingBottom: spacing.sm,
  },
  searchPill: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginHorizontal: spacing.lg,
    paddingHorizontal: spacing.md,
    height: 36,
    borderRadius: radii.md,
    backgroundColor: colors.creamDeeper,
  },
  searchInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 15,
    color: colors.ink,
    padding: 0,
  },
  error: {
    ...type.body,
    color: colors.errorText,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
  },
  list: { paddingHorizontal: spacing.sm, paddingVertical: spacing.sm },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: spacing.sm,
    borderRadius: radii.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  name: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.ink,
  },
  dim: { ...type.body, color: colors.mute, padding: spacing.lg, textAlign: "center" },
  action: {
    marginHorizontal: spacing.lg,
    height: 44,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  actionDisabled: { opacity: 0.5 },
  actionPressed: { opacity: 0.85 },
  actionLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 15,
    lineHeight: 20,
    color: colors.cream,
  },
});
