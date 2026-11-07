import { useAuth } from "@clerk/expo";
import { SymbolView } from "../ui/SymbolView";
import { agentDisplayName, useAgents, type MemberRef } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, type, useChatPalette } from "../../config/theme";
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

// Mirrors the web dialog's own participant cap (`RoomDirectory.tsx`'s
// `canCreate`'s `selected.size <= 31`, and the picker checkbox's
// `disabled={!selection && selected.size >= 31}`), which itself mirrors a
// real backend constraint: `MAX_ROOM_MEMBERS = 32` in
// `core/repositories/messaging_repo.py` (32 total room members = the 31
// picked here + the creator, added automatically), enforced as a 409
// `MEMBER_LIMIT` in `core/services/messaging.py`. A field that accepts more
// than the backend will take is exactly the defect this cap exists to close.
const MAX_ROOM_MEMBERS = 31;

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
  const palette = useChatPalette();
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
  // screen's own hook, so this costs no extra fetch. Both calls below pass
  // `activate: false` — this sheet stays mounted behind the room it opens, and
  // an instance holding a room it never displays would poll that room's detail
  // every 60s and reconcile its history behind the screen that owns it.
  //
  // ponytail: it is still a SECOND room machine, so every live event runs two
  // handlers and there are two AppState listeners. The upgrade is the parent
  // passing its own instance's `selectDm`/`createRoom` down as props; do it if
  // a busy workspace ever shows list churn.
  const { selectDm, createRoom } = useMobileRooms({
    autoSelect: false,
    subscribeToRoom: false,
  });
  const [people, setPeople] = useState<Candidate[]>([]);
  const [picked, setPicked] = useState<MemberRef[]>([]);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Opening is what loads the roster and what clears the last visit's picks,
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
    setPicked((current) => {
      const alreadyPicked = current.some((entry) => same(entry, ref));
      // A new pick past the cap is a no-op — mirrors the web dialog's
      // checkbox `disabled={!selection && selected.size >= 31}`. Enforced
      // here (not just via the row's `disabled` prop below) so the cap holds
      // regardless of how the press reached this callback.
      if (!alreadyPicked && current.length >= MAX_ROOM_MEMBERS) return current;
      return alreadyPicked
        ? current.filter((entry) => !same(entry, ref))
        : [...current, { kind: ref.kind, id: ref.id }];
    });
  }, []);


  // `picked.length <= MAX_ROOM_MEMBERS` is defensive — `toggle` already
  // refuses a pick past the cap — the same belt-and-suspenders shape as the
  // web dialog's `canCreate`, which checks `selected.size <= 31` even though
  // its own checkbox is also disabled past that count.
  const canStart =
    !busy &&
    picked.length <= MAX_ROOM_MEMBERS &&
    picked.length > 0;
  const actionLabel = picked.length > 1 ? "Create group" : "Message";

  const start = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      let roomId: string | null;
      if (picked.length === 1) {
        roomId = await selectDm(picked[0], { activate: false });
      } else {
        const created = await createRoom({
          clientRoomId: randomUUID(),
          kind: "group-dm",
          name: null,
          members: picked.map((ref) => ({
            kind: ref.kind,
            id: ref.id,
            role: "member" as const,
            listenMode: ref.kind === "agent" ? ("active" as const) : null,
          })),
        }, { activate: false });
        roomId = created.roomId;
      }
      if (roomId) onOpenRoom(roomId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The chat could not be opened.");
    } finally {
      setBusy(false);
    }
  }, [createRoom, onOpenRoom, picked, selectDm]);

  return (
    <BottomSheet
      visible={visible}
      onClose={onClose}
      large
    >
      <View style={[styles.card, { backgroundColor: palette.surface }]}>
        <Text style={[styles.overline, { color: palette.fgMute }]}>NEW MESSAGE</Text>
        <View style={[styles.searchPill, { backgroundColor: palette.raised }]}>
          <SymbolView accessibilityElementsHidden name="magnifyingglass" size={14} tintColor={palette.fgMute} />
          <TextInput
            style={[styles.searchInput, { color: palette.fg }]}
            placeholder="Search people and agents"
            placeholderTextColor={palette.fgMute}
            value={query}
            onChangeText={setQuery}
            autoCorrect={false}
            autoCapitalize="none"
          />
        </View>
        {error ? <Text style={[styles.error, { color: palette.alert }]}>{error}</Text> : null}
        <FlatList
          data={candidates}
          keyExtractor={(candidate) => `${candidate.kind}:${candidate.id}`}
          contentContainerStyle={styles.list}
          keyboardShouldPersistTaps="handled"
          // A workspace roster is small enough that virtualizing it away
          // trades a real cost (blank rows mid-scroll) for a saving that
          // doesn't matter at this size.
          initialNumToRender={50}
          ListEmptyComponent={
            <Text style={[styles.dim, { color: palette.fgMute }]}>
              {query.trim() ? "Nobody matches that." : "No one to message yet."}
            </Text>
          }
          renderItem={({ item }) => {
            const chosen = picked.some((entry) => same(entry, item));
            // Mirrors the web dialog's checkbox `disabled={!selection &&
            // selected.size >= 31}` — an already-picked row stays togglable
            // (you can still deselect it), only a NEW pick past the cap is
            // blocked.
            const atCap = !chosen && picked.length >= MAX_ROOM_MEMBERS;
            return (
              <Pressable
                onPress={() => toggle(item)}
                disabled={atCap}
                accessibilityRole="button"
                accessibilityState={{ selected: chosen, disabled: atCap }}
                style={({ pressed }) => [
                  styles.row,
                  atCap && styles.rowDisabled,
                  pressed && { backgroundColor: palette.hover },
                ]}
              >
                <Avatar name={item.displayName} kind={item.kind} emoji={item.emoji} palette={palette} />
                <Text style={[styles.name, { color: palette.fg }]} numberOfLines={1}>
                  {item.displayName}
                </Text>
                {chosen ? <SymbolView accessibilityElementsHidden name="checkmark" size={16} tintColor={palette.fg} /> : null}
              </Pressable>
            );
          }}
        />
        <Pressable
          onPress={() => void start()}
          disabled={!canStart}
          accessibilityRole="button"
          accessibilityLabel="Start chat"
          accessibilityState={{ disabled: !canStart }}
          style={({ pressed }) => [
            styles.action,
            { backgroundColor: palette.fg },
            !canStart && styles.actionDisabled,
            pressed && styles.actionPressed,
          ]}
        >
          <Text style={[styles.actionLabel, { color: palette.onFg }]}>{actionLabel}</Text>
        </Pressable>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    // Fills the full-height sheet: the list takes the room and the footer
    // stays at the bottom — above the keyboard while typing.
    flex: 1,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingBottom: spacing.md,
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
  },
  searchInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 15,
    padding: 0,
  },
  error: {
    ...type.body,
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
  rowDisabled: { opacity: 0.5 },
  name: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
  },
  dim: { ...type.body, padding: spacing.lg, textAlign: "center" },
  action: {
    marginHorizontal: spacing.lg,
    height: 44,
    borderRadius: radii.md,
    alignItems: "center",
    justifyContent: "center",
  },
  actionDisabled: { opacity: 0.5 },
  actionPressed: { opacity: 0.85 },
  actionLabel: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 15,
    lineHeight: 20,
  },
});
