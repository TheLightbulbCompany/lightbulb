import Feather from "@expo/vector-icons/Feather";
import { router } from "expo-router";
import { useMemo, useState, type ReactElement } from "react";
import {
  Animated,
  Linking,
  Pressable,
  RefreshControl,
  SectionList,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import {
  BOARD_STATUSES,
  STATUS_LABELS,
  resolveAssignee,
  statusColors,
  subtaskCounts,
  toBoardStatus,
  useTeamsBoard,
  type BoardIssue,
  type BoardStatus,
  type CompanyMember,
  type TeamsAgent,
} from "../../lib/teamsData";
import { usePulse } from "../chat/usePulse";
import { IssueCard } from "./IssueCard";
import { StatusSheet } from "./StatusSheet";
import { teamsPanelUrl } from "../../lib/webLinks";

// ---------------------------------------------------------------------------
// Board mode — status-grouped vertical sections (recommended phone layout,
// contract §6). Each non-empty status column is a section; tap a card → detail,
// long-press or the move affordance → status sheet → optimistic PATCH.
// ---------------------------------------------------------------------------

interface BoardSection {
  status: BoardStatus;
  data: BoardIssue[];
}

// Stable empty fallbacks so the derived `issues`/`agents` keep a constant
// reference between renders (avoids re-running the grouping memos needlessly).
const EMPTY_ISSUES: BoardIssue[] = [];
const EMPTY_AGENTS: TeamsAgent[] = [];
const EMPTY_MEMBERS: CompanyMember[] = [];

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

function SkeletonCards() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2].map((i) => (
        <Animated.View key={i} style={[styles.skeletonCard, { opacity: pulse }]} />
      ))}
    </View>
  );
}

function CenteredState({
  glyph,
  title,
  detail,
  action,
  pulsing,
}: {
  glyph?: ReactElement;
  title: string;
  detail?: string | null;
  action?: ReactElement;
  pulsing?: boolean;
}) {
  return (
    <View style={styles.centered}>
      {pulsing ? <PulsingDot /> : glyph}
      <Text style={styles.stateTitle}>{title}</Text>
      {detail != null && detail !== "" && (
        <Text style={styles.stateDetail}>{detail}</Text>
      )}
      {action}
    </View>
  );
}

function OutlineButton({ label, onPress }: { label: string; onPress: () => void }) {
  return (
    <Pressable
      style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Text style={styles.buttonText}>{label}</Text>
    </Pressable>
  );
}

function SectionHeader({ status, count }: { status: BoardStatus; count: number }) {
  const { color } = statusColors(status);
  return (
    <View style={styles.sectionHeader}>
      <View style={[styles.sectionDot, { backgroundColor: color }]} />
      <Text style={styles.sectionLabel}>{STATUS_LABELS[status].toUpperCase()}</Text>
      <Text style={styles.sectionCount}>{count}</Text>
    </View>
  );
}

export function BoardView() {
  const { state, refreshing, retry, refresh, moveIssue } = useTeamsBoard();
  const [moving, setMoving] = useState<BoardIssue | null>(null);

  const issues = useMemo(
    () => (state.kind === "ready" ? state.issues : EMPTY_ISSUES),
    [state],
  );
  const agents: TeamsAgent[] = state.kind === "ready" ? state.agents : EMPTY_AGENTS;
  const members: CompanyMember[] =
    state.kind === "ready" ? state.members : EMPTY_MEMBERS;

  const counts = useMemo(() => subtaskCounts(issues), [issues]);
  const sections = useMemo<BoardSection[]>(() => {
    const byStatus = new Map<BoardStatus, BoardIssue[]>();
    for (const issue of issues) {
      const key = toBoardStatus(issue.status);
      if (!key) continue; // off-board statuses are dropped (contract §1)
      const bucket = byStatus.get(key);
      if (bucket) bucket.push(issue);
      else byStatus.set(key, [issue]);
    }
    return BOARD_STATUSES.map((status) => ({
      status,
      data: byStatus.get(status) ?? [],
    })).filter((section) => section.data.length > 0);
  }, [issues]);

  if (state.kind === "loading") {
    return (
      <View style={styles.page}>
        <SkeletonCards />
      </View>
    );
  }

  if (state.kind === "gate") {
    if (state.gate === "provisioning") {
      return (
        <View style={styles.page}>
          <CenteredState
            pulsing
            title="Setting up your workspace…"
            detail="Your team’s agents are being provisioned. This usually takes under a minute."
            action={<OutlineButton label="Refresh" onPress={retry} />}
          />
        </View>
      );
    }
    if (state.gate === "needs_subscription") {
      return (
        <View style={styles.page}>
          <CenteredState
            glyph={<Feather name="lock" size={28} color={colors.mute} />}
            title="Teams requires an active subscription"
            detail="Manage your subscription on isol8.co to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on isol8.co"
                onPress={() => void Linking.openURL(teamsPanelUrl("issues"))}
              />
            }
          />
        </View>
      );
    }
    return (
      <View style={styles.page}>
        <CenteredState
          glyph={<Feather name="cloud-off" size={28} color={colors.mute} />}
          title="Couldn’t reach your workspace"
          detail={state.message}
          action={<OutlineButton label="Retry" onPress={retry} />}
        />
      </View>
    );
  }

  const empty = sections.length === 0;

  return (
    <>
      <SectionList<BoardIssue, BoardSection>
        style={styles.page}
        sections={sections}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[styles.listContent, empty && styles.grow]}
        stickySectionHeadersEnabled={false}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={refresh}
            tintColor={colors.mute}
          />
        }
        renderSectionHeader={({ section }) => (
          <SectionHeader status={section.status} count={section.data.length} />
        )}
        renderItem={({ item }) => (
          <IssueCard
            issue={item}
            assignee={resolveAssignee(item, agents, members)}
            subtaskCount={counts[item.id] ?? 0}
            onPress={() =>
              router.push({
                pathname: "/(app)/teams/issue/[id]",
                params: { id: item.id, title: item.title },
              })
            }
            onMove={() => setMoving(item)}
          />
        )}
        ItemSeparatorComponent={() => <View style={styles.cardGap} />}
        SectionSeparatorComponent={() => <View style={styles.sectionGap} />}
        ListEmptyComponent={
          <CenteredState
            glyph={<Text style={styles.emptyGlyph}>8</Text>}
            title="No issues"
            detail="Issues on your board will show up here."
          />
        }
      />

      <StatusSheet
        visible={moving != null}
        issueTitle={moving?.title ?? null}
        currentStatus={moving?.status}
        onSelect={(status) => {
          if (moving) void moveIssue(moving.id, status);
        }}
        onClose={() => setMoving(null)}
      />
    </>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
  },
  grow: { flexGrow: 1, justifyContent: "center" },

  cardGap: { height: spacing.sm },
  sectionGap: { height: spacing.lg },

  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.xs,
    paddingBottom: spacing.sm,
  },
  sectionDot: { width: 9, height: 9, borderRadius: 5 },
  sectionLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
  },
  sectionCount: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1,
    color: colors.sectionCount,
  },

  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // States.
  centered: {
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    padding: spacing.xl,
  },
  pulseDot: {
    width: 12,
    height: 12,
    borderRadius: 6,
    backgroundColor: colors.forestSoft,
  },
  stateTitle: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    textAlign: "center",
  },
  stateDetail: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 19,
    color: colors.mute,
    textAlign: "center",
  },
  button: {
    marginTop: spacing.xs,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  buttonPressed: { opacity: 0.6 },
  buttonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },

  skeletonGroup: {
    gap: spacing.md,
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
  },
  skeletonCard: {
    height: 96,
    borderRadius: radii.lg,
    backgroundColor: colors.skeleton,
  },
});
