import { useCallback, useMemo, useState, type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useStatusLoaded, useRuntimeRpc } from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { useLiveEvents } from "../../../providers/live-events";
import { LogoMark } from "../../../components/LogoMark";

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §3 Skills)
//
// skills.status (no params = global). Payload is Skill[] OR
// {workspaceDir?, managedSkillsDir?, skills:[…]} — normalize both. Read-only
// for v1: install/update mutations exist server-side but are HIGH-risk
// (skills.install runs a 120s binary fetch, skills.update toggles runtime
// config) and are deferred to a later build.
// ---------------------------------------------------------------------------

interface InstallSpec {
  id?: string;
  kind?: string;
  label?: string;
  bins?: string[];
}

interface RawSkill {
  name?: string;
  description?: string;
  emoji?: string;
  skillKey?: string;
  disabled?: boolean;
  always?: boolean;
  missing?: { bins?: string[]; env?: string[]; config?: string[]; os?: string[] };
  install?: InstallSpec[];
}

type SkillState = "installed" | "available" | "unsupported";

interface Skill {
  key: string;
  name: string;
  description: string | null;
  emoji: string | null;
  state: SkillState;
  missingBins: string[];
  missingEnv: string[];
}

function normalizeSkills(payload: unknown): RawSkill[] {
  if (Array.isArray(payload)) return payload as RawSkill[];
  const wrapped = (payload as { skills?: RawSkill[] } | null)?.skills;
  return Array.isArray(wrapped) ? wrapped : [];
}

/** A `brew` install spec is dead weight in the cloud container (no Homebrew),
 * so it never counts toward viability — matches the web's brew filtering. */
function isBrewSpec(spec: InstallSpec): boolean {
  return (
    spec.kind === "brew" ||
    /brew/i.test(spec.id ?? "") ||
    /brew/i.test(spec.label ?? "")
  );
}

/** State machine (contract §3, SkillsTab:138-159): no missing bins →
 * disabled?available:installed; missing bins + a viable non-brew install spec
 * → available; else unsupported ("Desktop only"). */
function deriveSkillState(skill: RawSkill): SkillState {
  const missingBins = skill.missing?.bins ?? [];
  if (missingBins.length === 0) {
    return skill.disabled ? "available" : "installed";
  }
  const hasViableInstall = (skill.install ?? []).some(
    (spec) => !isBrewSpec(spec),
  );
  return hasViableInstall ? "available" : "unsupported";
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
      state: deriveSkillState(row),
      missingBins: row.missing?.bins ?? [],
      missingEnv: row.missing?.env ?? [],
    });
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

// ---------------------------------------------------------------------------
// State → badge tokens (§3 tool-pill trio adaptation)
// ---------------------------------------------------------------------------

const STATE_META: Record<
  SkillState,
  { label: string; bg: string; text: string; border: string }
> = {
  installed: {
    label: "Installed",
    bg: colors.tool.running.bg,
    text: colors.tool.running.text,
    border: colors.tool.running.border,
  },
  // Available and unsupported are both neutral — mute is not a state — so they
  // separate by surface step and ink, not by hue: available reads at full
  // strength on the deeper step, unsupported recedes to `mute` on the lighter one.
  available: {
    label: "Available",
    bg: colors.creamDeepest,
    text: colors.ink,
    border: colors.edge,
  },
  unsupported: {
    label: "Desktop only",
    bg: colors.creamDeeper,
    text: colors.mute,
    border: colors.edge,
  },
};

const SECTION_ORDER: SkillState[] = ["installed", "available", "unsupported"];

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
  const meta = STATE_META[state];
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
          {STATE_META[item.state].label.toUpperCase()}
          <Text style={styles.sectionCount}> · {item.count}</Text>
        </Text>
      );
    }
    const { skill } = item;
    const missing = [...skill.missingBins, ...skill.missingEnv];
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
          {missing.length > 0 && (
            <Text style={styles.missing} numberOfLines={1}>
              Needs {missing.join(", ")}
            </Text>
          )}
        </View>
      </View>
    );
  }, []);

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
            tintColor={colors.forest}
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

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
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
  dim: { ...type.body, color: colors.mute, textAlign: "center" },

  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginTop: spacing.md,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

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
    backgroundColor: colors.creamDeeper,
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
    color: colors.ink,
  },
  description: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  missing: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    color: colors.warn,
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
    backgroundColor: colors.skeleton,
  },

  // Error card (spec §4).
  errorCard: {
    margin: spacing.md,
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    gap: spacing.md,
  },
  errorText: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  pressed: { opacity: 0.6 },
  retryButton: {
    alignSelf: "flex-start",
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
  },
  retryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
});
