import { useCallback, useMemo, useState, type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from "react-native";
import { useStatusLoaded, useRuntimeRpc } from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import { useLiveEvents } from "../../../providers/live-events";
import { LogoMark } from "../../../components/LogoMark";

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §3 Skills)
//
// skills.status (no params = global). Payload is Skill[] OR
// {workspaceDir?, managedSkillsDir?, skills:[…]} — normalize both. Read-only.
// Hermes has no per-skill dependency manifest, so a skill is simply on or off.
// ---------------------------------------------------------------------------

interface RawSkill {
  name?: string;
  description?: string;
  emoji?: string;
  skillKey?: string;
  disabled?: boolean;
  always?: boolean;
}

type SkillState = "installed" | "available";

interface Skill {
  key: string;
  name: string;
  description: string | null;
  emoji: string | null;
  state: SkillState;
}

function normalizeSkills(payload: unknown): RawSkill[] {
  if (Array.isArray(payload)) return payload as RawSkill[];
  const wrapped = (payload as { skills?: RawSkill[] } | null)?.skills;
  return Array.isArray(wrapped) ? wrapped : [];
}

function projectSkills(payload: unknown): Skill[] {
  const rows = normalizeSkills(payload);
  const out: Skill[] = [];
  for (const row of rows) {
    const name = (row.name ?? row.skillKey ?? "").trim();
    if (!name) continue;
    out.push({
      key: row.skillKey || name,
      name,
      description: row.description?.trim() || null,
      emoji: row.emoji?.trim() || null,
      state: row.disabled ? "available" : "installed",
    });
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

// ---------------------------------------------------------------------------
// State → badge tokens (§3 tool-pill trio adaptation)
// ---------------------------------------------------------------------------

function stateMeta(
  p: ChatPalette,
): Record<SkillState, { label: string; bg: string; text: string; border: string }> {
  return {
    installed: {
      label: "Installed",
      bg: `${p.live}22`,
      text: p.live,
      border: p.live,
    },
    available: {
      label: "Available",
      bg: p.raised,
      text: p.fg,
      border: p.edge,
    },
  };
}

const SECTION_ORDER: SkillState[] = ["installed", "available"];

type ListEntry =
  | { type: "header"; state: SkillState; count: number }
  | { type: "skill"; skill: Skill };

function buildEntries(skills: Skill[]): ListEntry[] {
  const entries: ListEntry[] = [];
  for (const state of SECTION_ORDER) {
    const group = skills.filter((s) => s.state === state);
    if (group.length === 0) continue;
    entries.push({ type: "header", state, count: group.length });
    for (const skill of group) entries.push({ type: "skill", skill });
  }
  return entries;
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function SkeletonRows() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2, 3].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

function StateBadge({ state }: { state: SkillState }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const meta = stateMeta(chatPalette[scheme])[state];
  return (
    <View
      style={[
        styles.badge,
        { backgroundColor: meta.bg, borderColor: meta.border },
      ]}
    >
      <Text style={[styles.badgeText, { color: meta.text }]}>{meta.label}</Text>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function SkillsPanel() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const meta = useMemo(() => stateMeta(palette), [palette]);
  const live = useLiveEvents();
  // `undefined` is "runtime not known for this workspace yet", never "this is
  // ECS" — firing on a guess costs a microVM start on AgentCore (M6 task 10-E).
  // The Hermes bridge answers the same shape this screen already projects, with
  // no dependency manifest, so every skill reads as installed or available.
  const statusLoaded = useStatusLoaded();
  const { data, error, isLoading, mutate } = useRuntimeRpc<unknown>(
    live,
    statusLoaded ? "skills.status" : null,
  );

  const skills = useMemo(() => projectSkills(data), [data]);
  const entries = useMemo(() => buildEntries(skills), [skills]);

  const [refreshing, setRefreshing] = useState(false);
  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await mutate();
    } finally {
      setRefreshing(false);
    }
  }, [mutate]);

  const showEmpty = skills.length === 0 && !isLoading && data != null;

  const renderItem = useCallback(({ item }: { item: ListEntry }) => {
    if (item.type === "header") {
      return (
        <Text style={styles.sectionLabel}>
          {meta[item.state].label.toUpperCase()}
          <Text style={styles.sectionCount}> · {item.count}</Text>
        </Text>
      );
    }
    const { skill } = item;
    return (
      <View style={styles.row}>
        <View style={styles.emojiTile}>
          <Text style={styles.emoji}>{skill.emoji ?? "•"}</Text>
        </View>
        <View style={styles.rowText}>
          <View style={styles.rowTop}>
            <Text style={styles.name} numberOfLines={1}>
              {skill.name}
            </Text>
            <StateBadge state={skill.state} />
          </View>
          {skill.description != null && (
            <Text style={styles.description} numberOfLines={2}>
              {skill.description}
            </Text>
          )}
        </View>
      </View>
    );
  }, [meta, styles]);

  let body: ReactElement;
  if (error) {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load skills: {error.message}
        </Text>
        <Pressable
          style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
          onPress={() => void mutate()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else {
    body = (
      <FlatList
        contentInsetAdjustmentBehavior="automatic"
        data={entries}
        keyExtractor={(e) =>
          e.type === "header" ? `h-${e.state}` : `s-${e.skill.key}`
        }
        style={styles.list}
        contentContainerStyle={[
          styles.listContent,
          showEmpty && styles.centerGrow,
        ]}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => void onRefresh()}
            tintColor={palette.fg}
          />
        }
        ListEmptyComponent={
          // `!statusLoaded` counts as loading: the RPC is held until the runtime
          // is known, and without it the held state reads as "unreachable".
          isLoading || !statusLoaded ? (
            <SkeletonRows />
          ) : !data ? (
            // Connected but the RPC resolved to undefined (the shared fetcher
            // maps "No container" → undefined) — a genuine unreachable state,
            // not an infinite load.
            <View style={styles.emptyWrap}>
              <Text style={styles.dim}>Couldn’t reach your container.</Text>
            </View>
          ) : (
            <View style={styles.emptyWrap}>
              <LogoMark size={48} />
              <Text style={styles.dim}>No skills found.</Text>
            </View>
          )
        }
        renderItem={renderItem}
      />
    );
  }

  return <View style={styles.page}>{body}</View>;
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    list: { flex: 1 },
    listContent: {
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
      paddingBottom: spacing.xl,
    },
    centerGrow: {
      flexGrow: 1,
      alignItems: "center",
      justifyContent: "center",
      padding: spacing.xl,
    },
    dim: {
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 22,
      color: p.fgMute,
      textAlign: "center",
    },

    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.sm,
      marginTop: spacing.md,
      marginBottom: spacing.sm,
    },
    sectionCount: { color: p.fgMute },

    // Skill rows.
    row: {
      flexDirection: "row",
      alignItems: "flex-start",
      gap: spacing.md,
      padding: spacing.sm,
      borderRadius: 10,
    },
    emojiTile: {
      width: 32,
      height: 32,
      borderRadius: radii.md,
      backgroundColor: p.raised,
      alignItems: "center",
      justifyContent: "center",
    },
    emoji: { fontSize: 16, lineHeight: 20 },
    rowText: { flex: 1, gap: 3 },
    rowTop: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: spacing.sm,
    },
    name: {
      flexShrink: 1,
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    description: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },

    // State badge (§3 tool-pill trio).
    badge: {
      borderRadius: radii.xs,
      borderWidth: 1,
      paddingHorizontal: spacing.xs,
      paddingVertical: 1,
    },
    badgeText: {
      fontFamily: fonts.mono,
      fontSize: 9,
      lineHeight: 12,
      letterSpacing: 0.6,
    },

    // Empty state (spec §7).
    emptyWrap: { alignItems: "center", gap: spacing.sm },

    // Skeletons (spec §7).
    skeletonGroup: { alignSelf: "stretch", gap: spacing.sm },
    skeletonRow: {
      height: 56,
      borderRadius: 10,
      backgroundColor: p.raised,
    },

    // Error card (spec §4).
    errorCard: {
      margin: spacing.md,
      padding: spacing.lg,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.md,
      gap: spacing.md,
    },
    errorText: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 20,
      color: p.alert,
    },
    pressed: { opacity: 0.6 },
    retryButton: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },
    retryText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.alert,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
