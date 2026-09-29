import { SymbolView } from "expo-symbols";
import { router } from "expo-router";
import { useMemo, useRef, useState, type ReactElement } from "react";
import {
  Animated,
  Pressable,
  RefreshControl,
  SectionList,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
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
import { LogoMark } from "../LogoMark";

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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

function SkeletonCards() {
  const pulse = usePulse(true, 0.55);
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const { color } = statusColors(status, scheme);
  const styles = SHEETS[scheme];
  return (
    <View style={styles.sectionHeader}>
      <View style={[styles.sectionDot, { backgroundColor: color }]} />
      <Text style={styles.sectionLabel}>{STATUS_LABELS[status].toUpperCase()}</Text>
      <Text style={styles.sectionCount}>{count}</Text>
    </View>
  );
}

export function BoardView() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const { state, refreshing, retry, refresh, moveIssue } = useTeamsBoard();
  const [moving, setMoving] = useState<BoardIssue | null>(null);
  // The sheet sizes to its content, so it keeps showing the last issue while
  // it animates away instead of shrinking mid-exit.
  const lastMoving = useRef<BoardIssue | null>(null);
  if (moving) lastMoving.current = moving;

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
            glyph={<SymbolView accessibilityElementsHidden name="lock" size={28} tintColor={palette.fgMute} />}
            title="Teams isn't available for this workspace right now."
          />
        </View>
      );
    }
    return (
      <View style={styles.page}>
        <CenteredState
          glyph={<SymbolView accessibilityElementsHidden name="network.slash" size={28} tintColor={palette.fgMute} />}
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
        contentInsetAdjustmentBehavior="automatic"
        style={styles.page}
        sections={sections}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[styles.listContent, empty && styles.grow]}
        stickySectionHeadersEnabled={false}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={refresh}
            tintColor={palette.fgMute}
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
            glyph={<LogoMark size={48} />}
            title="No issues"
            detail="Issues on your board will show up here."
          />
        }
      />

      <StatusSheet
        visible={moving != null}
        issueTitle={lastMoving.current?.title ?? null}
        currentStatus={lastMoving.current?.status}
        onSelect={(status) => {
          if (moving) void moveIssue(moving.id, status);
        }}
        onClose={() => setMoving(null)}
      />
    </>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
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
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.4,
      color: p.fgMute,
    },
    sectionCount: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1,
      color: p.fgMute,
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
      backgroundColor: p.live,
    },
    stateTitle: {
      fontFamily: fonts.sansMedium,
      fontSize: 15,
      lineHeight: 20,
      color: p.fg,
      textAlign: "center",
    },
    stateDetail: {
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 19,
      color: p.fgMute,
      textAlign: "center",
    },
    button: {
      marginTop: spacing.xs,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.lg,
      paddingVertical: spacing.sm,
    },
    buttonPressed: { opacity: 0.6 },
    buttonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.link,
    },

    skeletonGroup: {
      gap: spacing.md,
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
    },
    skeletonCard: {
      height: 96,
      borderRadius: radii.lg,
      backgroundColor: p.raised,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
