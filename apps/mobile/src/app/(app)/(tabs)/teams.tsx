import { SymbolView } from "../../../components/ui/SymbolView";
import { router } from "expo-router";
import { useContext, useEffect, useState, type ReactElement } from "react";
import { HeaderHeightContext } from "expo-router/react-navigation";
import {
  Animated,
  FlatList,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  useColorScheme,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { ApprovalCard } from "../../../components/teams/ApprovalCard";
import { BoardView } from "../../../components/teams/BoardView";
import { CreateIssueSheet } from "../../../components/teams/CreateIssueSheet";
import { insetRowStyle } from "../../../components/ui/InsetGroup";
import { ListRow } from "../../../components/ui/ListRow";
import { SegmentedControl } from "../../../components/ui/SegmentedControl";
import { WorkScreen } from "../../../components/tasks/WorkScreen";
import { useSuperAgent } from "../../../providers/live-events";
import { chatPalette, colors, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import {
  formatCents,
  humanizeStatus,
  isApprovalActionable,
  runDayTotal,
  timeAgo,
  totalPendingApprovals,
  useCreateTeamIssue,
  useTeamsApprovals,
  useTeamsDashboard,
  useTeamsInbox,
  INBOX_TABS,
  INBOX_TAB_LABELS,
  type CreateIssueInput,
  type FailedRun,
  type InboxItem,
  type InboxTab,
  type RunActivityDay,
  type TeamsAgent,
} from "../../../lib/teamsData";
import { LogoMark } from "../../../components/LogoMark";
import { useTabHeader } from "../../../lib/tabHeader";

// Teams is a subscription-gated surface. App Store 3.1.1/3.1.3(f): the app
// must carry no purchase call to action, so a workspace without an active
// subscription just sees plain, neutral state — no button, no link out.

/** Light/dark stylesheets, built once — never per render, never per component. */
function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    host: { flex: 1, backgroundColor: p.ground },

    // Header "+" (board mode only).
    headerBtn: {
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
      borderRadius: radii.sm,
    },
    headerBtnPressed: { opacity: 0.55 },

    // The mode switcher's strip.
    toggleBar: {
      paddingHorizontal: spacing.md,
      paddingTop: spacing.sm,
      paddingBottom: spacing.sm,
      backgroundColor: p.ground,
    },

    page: { flex: 1, backgroundColor: p.ground },
    listContent: {
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
      paddingBottom: spacing.xl,
    },
    // Inbox rows carry their own 16px inset via insetRowStyle (Activity's
    // convention) — a container-level paddingHorizontal here would stack
    // with it and push rows in further than the failed-runs band/label.
    insetListContent: {
      paddingTop: spacing.md,
      paddingBottom: spacing.xl,
    },
    grow: { flexGrow: 1, justifyContent: "center" },

    // Section label (spec §5).
    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.sm,
      marginBottom: spacing.sm,
    },
    // Matches the inbox row's 16px inset now that the container carries none.
    sectionLabelInset: { paddingHorizontal: 16 },
    sectionCount: { color: p.fgMute },

    // Approvals list — spacing between approval cards.
    approvalItem: { marginBottom: spacing.md },

    // Inbox controls: tab chips + search (web InboxToolbar parity, phone-sized).
    inboxControls: {
      paddingHorizontal: spacing.md,
      paddingBottom: spacing.sm,
      gap: spacing.sm,
    },
    tabRow: { flexDirection: "row", gap: spacing.xs },
    tabChip: {
      paddingHorizontal: spacing.md,
      paddingVertical: 6,
      borderRadius: radii.xl,
      backgroundColor: p.raised,
    },
    tabChipActive: {
      backgroundColor: p.surface,
      borderWidth: 1,
      borderColor: p.edge,
    },
    tabChipText: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },
    tabChipTextActive: { color: p.fg },
    searchBox: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.md,
      paddingHorizontal: spacing.md,
      paddingVertical: 6,
    },
    searchInput: {
      flex: 1,
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
      padding: 0,
    },

    // Failed-runs band (web NeedsAttentionBand parity).
    failedBand: {
      backgroundColor: colors.errorBg,
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.md,
      padding: spacing.md,
      marginHorizontal: 16,
      marginBottom: spacing.md,
      gap: spacing.sm,
    },
    failedBandLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.4,
      color: p.alert,
    },
    failedRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
    },
    failedRowText: { flex: 1, gap: 2 },
    failedRowTitle: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fg,
    },
    failedRowAction: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.link,
    },
    failedRowActionMuted: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },
    // Shared with the pressed state of `linkRow` below.
    rowPressed: { backgroundColor: p.selected },

    // Overview → Routines link row.
    linkRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
    },
    linkRowText: {
      flex: 1,
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 19,
      color: p.fg,
    },

    // Shared by FailedRunRow's timestamp.
    metaLine: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },

    // Readiness / empty / error states.
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

    // Buttons.
    button: {
      marginTop: spacing.xs,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.lg,
      paddingVertical: spacing.sm,
    },
    buttonDanger: { borderColor: p.alert },
    buttonPressed: { opacity: 0.6 },
    buttonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.link,
    },
    buttonTextDanger: { color: p.alert },

    // Loading skeletons (spec §7).
    skeletonGroup: {
      gap: spacing.sm,
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
    },
    skeletonRow: {
      height: 56,
      borderRadius: radii.md,
      backgroundColor: p.raised,
    },

    // Dashboard / Overview (contract §3).
    dashContent: {
      paddingHorizontal: spacing.md,
      paddingTop: spacing.md,
      paddingBottom: spacing.xl,
      gap: spacing.md,
    },
    dashGrid: {
      flexDirection: "row",
      flexWrap: "wrap",
      gap: spacing.md,
    },
    dashTile: {
      flexGrow: 1,
      flexBasis: "47%",
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
      gap: spacing.sm,
    },
    dashTilePressed: { opacity: 0.6 },
    dashTileLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.4,
      color: p.fgMute,
    },
    dashTileValue: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 20,
      lineHeight: 26,
      color: p.fg,
    },
    dashSkeletonLabel: {
      height: 10,
      width: "60%",
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },
    dashSkeletonValue: {
      height: 22,
      width: "45%",
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },

    // Run-activity summary card (lean bar row; NO chart library).
    activityCard: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
      gap: spacing.md,
    },
    activityHeader: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
    },
    activityTotal: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    barRow: {
      flexDirection: "row",
      alignItems: "flex-end",
      justifyContent: "space-between",
      height: 40,
      gap: spacing.xs,
    },
    bar: {
      flex: 1,
      borderRadius: radii.xs,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };

// ---------------------------------------------------------------------------
// Readiness affordance (spec §7) — pulsing dot for the provisioning state.
// ---------------------------------------------------------------------------

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

function SkeletonRows() {
  const pulse = usePulse(true, 0.55);
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2, 3].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Inbox row (spec §5) — a grouped-inset ListRow, one per issue.
// ---------------------------------------------------------------------------

function InboxRow({ item, index, count }: { item: InboxItem; index: number; count: number }) {
  const palette = chatPalette[useColorScheme() === "dark" ? "dark" : "light"];
  const identifier = item.identifier ?? item.id.slice(0, 8);
  const when = timeAgo(item.updatedAt ?? item.lastActivityAt ?? item.createdAt);
  const meta = [identifier, when].filter(Boolean).join(" · ");
  const preview = item.status ? humanizeStatus(item.status) : null;

  return (
    <View style={insetRowStyle(palette, index, count)}>
      <ListRow
        palette={palette}
        leading={null}
        title={item.title}
        time={when}
        preview={[identifier, preview].filter(Boolean).join(" · ")}
        previewLines={1}
        unread={!!item.unread}
        onPress={() =>
          router.push({
            pathname: "/(app)/teams/issue/[id]",
            params: { id: item.id, title: item.title },
          })
        }
        accessibilityLabel={`${item.title}. ${meta}`}
      />
    </View>
  );
}

// ---------------------------------------------------------------------------
// Readiness / empty / error states (contract §1 — 202/402/503 are NOT errors).
// ---------------------------------------------------------------------------

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

function OutlineButton({
  label,
  onPress,
  danger,
}: {
  label: string;
  onPress: () => void;
  danger?: boolean;
}) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <Pressable
      style={({ pressed }) => [
        styles.button,
        danger && styles.buttonDanger,
        pressed && styles.buttonPressed,
      ]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Text style={[styles.buttonText, danger && styles.buttonTextDanger]}>
        {label}
      </Text>
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Modes — Overview, Inbox (flat assigned list), Board (status-grouped,
// movable) and Approvals, switched by iOS's own segmented control.
// ---------------------------------------------------------------------------

type TeamsMode = "overview" | "inbox" | "board" | "approvals";

const MODES: readonly { key: TeamsMode; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "inbox", label: "Inbox" },
  { key: "board", label: "Board" },
  { key: "approvals", label: "Approvals" },
];

// ---------------------------------------------------------------------------
// Screen — hosts the mode switcher and the four modes.
// ---------------------------------------------------------------------------

export default function TeamsScreen() {
  const superAgent = useSuperAgent();
  // With the super-agent on, the tab is Work: the threads overview and the schedules. Today's board stays
  // reachable from a link there; off, this screen is today's.
  const [oldBoard, setOldBoard] = useState(false);
  if (superAgent && !oldBoard) return <WorkScreen onOpenOldBoard={() => setOldBoard(true)} />;
  return <TeamsBoard onBackToWork={superAgent ? () => setOldBoard(false) : undefined} />;
}

function TeamsBoard({ onBackToWork }: { onBackToWork?: () => void }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const [mode, setMode] = useState<TeamsMode>("overview");
  const [createOpen, setCreateOpen] = useState(false);
  const createIssue = useCreateTeamIssue();

  // The "+" (new task) is available from EVERY mode for discoverability.
  // It opens in-place; only after a successful write do we move the user to the
  // new issue (or the board if the backend did not return an id).
  const openCreate = () => {
    setCreateOpen(true);
  };
  const submitCreate = async (input: CreateIssueInput): Promise<boolean> => {
    const created = await createIssue(input);
    if (!created) return false;
    setCreateOpen(false);
    if (created.id) {
      router.push({
        pathname: "/(app)/teams/issue/[id]",
        params: { id: created.id, title: created.title ?? input.title },
      });
    } else {
      setMode("board");
    }
    return true;
  };

  // Teams is one of the four tabs now, sharing the (app) Stack's header — see
  // lib/tabHeader.ts. `openCreate` only calls the stable `setCreateOpen`
  // setter, so the closure useTabHeader keeps from the last selection never
  // goes stale.
  useTabHeader({
    headerRight: () => (
      <Pressable
        onPress={openCreate}
        hitSlop={10}
        style={({ pressed }) => [
          styles.headerBtn,
          pressed && styles.headerBtnPressed,
        ]}
        accessibilityRole="button"
        accessibilityLabel="New task"
      >
        <SymbolView accessibilityElementsHidden name="plus" size={22} tintColor={palette.fg} />
      </Pressable>
    ),
  });

  // The tabs share one translucent header, and this root is a plain View, so it
  // lays out UNDER the header — the Overview/Inbox/Board/Approvals switcher sat
  // hidden behind it. The lists below it inset themselves (automatic insets).
  const headerHeight = useContext(HeaderHeightContext) ?? 0;
  return (
    <View style={[styles.host, { paddingTop: headerHeight }]} testID="teams-host">
      {onBackToWork ? (
        <Pressable accessibilityRole="button" accessibilityLabel="Back to Work" onPress={onBackToWork} style={{ paddingHorizontal: spacing.lg, paddingVertical: spacing.sm }}>
          <Text style={{ fontFamily: fonts.sans, fontSize: 14, color: palette.link }}>‹ Work</Text>
        </Pressable>
      ) : null}
      <View style={styles.toggleBar}>
        <SegmentedControl
          label="Teams view"
          segments={MODES}
          value={mode}
          onChange={setMode}
          colorScheme={scheme}
        />
      </View>
      {mode === "overview" ? (
        <DashboardView onOpenApprovals={() => setMode("approvals")} />
      ) : mode === "inbox" ? (
        <InboxView />
      ) : mode === "approvals" ? (
        <ApprovalsView />
      ) : (
        <BoardView />
      )}
      <CreateIssueSheet
        visible={createOpen}
        mode="issue"
        onSubmit={submitCreate}
        onClose={() => setCreateOpen(false)}
      />
    </View>
  );
}

// ---------------------------------------------------------------------------
// Dashboard / Overview view (contract §3) — a glanceable set of ~4 stat tiles
// (agents active, open tasks, pending approvals, month spend) from
// `GET /teams/dashboard`, plus a lean run-activity summary. Same 202/402/503
// readiness gate as the inbox. Pending-approvals tile deep-links to Approvals.
// ---------------------------------------------------------------------------

/** 2-col stat tile mirroring control/overview.tsx: overline label + big number. */
function DashTile({
  label,
  value,
  onPress,
}: {
  label: string;
  value: string;
  onPress?: () => void;
}) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const content = (
    <>
      <Text style={styles.dashTileLabel}>{label}</Text>
      <Text style={styles.dashTileValue} numberOfLines={1}>
        {value}
      </Text>
    </>
  );
  if (onPress) {
    return (
      <Pressable
        style={({ pressed }) => [styles.dashTile, pressed && styles.dashTilePressed]}
        onPress={onPress}
        accessibilityRole="button"
        accessibilityLabel={`${label}: ${value}`}
      >
        {content}
      </Pressable>
    );
  }
  return (
    <View style={styles.dashTile} accessibilityLabel={`${label}: ${value}`}>
      {content}
    </View>
  );
}

function DashboardSkeleton() {
  const pulse = usePulse(true, 0.55);
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <View style={styles.dashGrid}>
      {[0, 1, 2, 3].map((i) => (
        <View key={i} style={styles.dashTile}>
          <Animated.View style={[styles.dashSkeletonLabel, { opacity: pulse }]} />
          <Animated.View style={[styles.dashSkeletonValue, { opacity: pulse }]} />
        </View>
      ))}
    </View>
  );
}

/**
 * Lean run-activity summary — a "runs this week" total plus a minimal inline bar
 * row over the last 14 days (plain RN Views, NO chart library). Each bar's height
 * scales to the busiest day; empty days render a short muted stub.
 */
function RunActivityCard({ days }: { days: RunActivityDay[] }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const recent = days.slice(-14);
  const totals = recent.map(runDayTotal);
  const max = Math.max(1, ...totals);
  const weekTotal = totals.slice(-7).reduce((sum, t) => sum + t, 0);
  return (
    <View style={styles.activityCard}>
      <View style={styles.activityHeader}>
        <Text style={styles.dashTileLabel}>RUN ACTIVITY</Text>
        <Text style={styles.activityTotal}>{weekTotal} this week</Text>
      </View>
      <View
        style={styles.barRow}
        accessibilityLabel={`${weekTotal} agent runs in the last week`}
      >
        {recent.map((day, i) => {
          const total = totals[i];
          const height = 4 + Math.round((total / max) * 36);
          return (
            <View
              key={day.date ?? i}
              style={[
                styles.bar,
                {
                  height,
                  backgroundColor: total > 0 ? palette.live : palette.raised,
                },
              ]}
            />
          );
        })}
      </View>
    </View>
  );
}

function DashboardView({ onOpenApprovals }: { onOpenApprovals: () => void }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const { state, refreshing, retry, refresh } = useTeamsDashboard();

  if (state.kind === "loading") {
    return (
      <View style={styles.page}>
        <View style={styles.dashContent}>
          <DashboardSkeleton />
        </View>
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

  // ready
  const dashboard = state.dashboard;
  const agentsActive = dashboard.agents?.active ?? 0;
  const openTasks = dashboard.tasks?.open ?? 0;
  const pending = totalPendingApprovals(dashboard);
  const spendCents = dashboard.costs?.monthSpendCents;
  const activity = Array.isArray(dashboard.runActivity)
    ? dashboard.runActivity
    : [];

  return (
    <ScrollView
      contentInsetAdjustmentBehavior="automatic"
      style={styles.page}
      contentContainerStyle={styles.dashContent}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={refresh}
          tintColor={palette.fgMute}
        />
      }
    >
      <Text style={styles.sectionLabel}>OVERVIEW</Text>
      <View style={styles.dashGrid}>
        <DashTile label="AGENTS" value={String(agentsActive)} />
        <DashTile label="OPEN TASKS" value={String(openTasks)} />
        <DashTile
          label="PENDING APPROVALS"
          value={String(pending)}
          onPress={pending > 0 ? onOpenApprovals : undefined}
        />
        {spendCents != null ? (
          <DashTile label="MONTH SPEND" value={formatCents(spendCents)} />
        ) : null}
      </View>
      {activity.length > 0 ? <RunActivityCard days={activity} /> : null}
      <Pressable
        style={({ pressed }) => [styles.linkRow, pressed && styles.rowPressed]}
        onPress={() => router.push("/(app)/teams/routines")}
        accessibilityRole="button"
        accessibilityLabel="Routines"
      >
        <SymbolView accessibilityElementsHidden name="repeat" size={16} tintColor={palette.link} />
        <Text style={styles.linkRowText}>Routines</Text>
        <SymbolView accessibilityElementsHidden name="chevron.right" size={16} tintColor={palette.fgMute} />
      </Pressable>
    </ScrollView>
  );
}

// ---------------------------------------------------------------------------
// Approvals view (contract §2) — global inbox of pending, actionable approvals
// with inline Approve / Reject. Same 202/402/503 readiness gate as the inbox.
// ---------------------------------------------------------------------------

function ApprovalsView() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const { state, refreshing, retry, refresh, approve, reject } = useTeamsApprovals();

  if (state.kind === "loading") {
    return (
      <View style={styles.page}>
        <SkeletonRows />
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

  // ready — the global inbox shows only actionable (pending / revision) rows.
  const pending = state.approvals.filter(isApprovalActionable);
  const empty = pending.length === 0;
  return (
    <FlatList
      contentInsetAdjustmentBehavior="automatic"
      style={styles.page}
      data={pending}
      keyExtractor={(item) => item.id}
      contentContainerStyle={[styles.listContent, empty && styles.grow]}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={refresh}
          tintColor={palette.fgMute}
        />
      }
      ListHeaderComponent={
        !empty ? (
          <Text style={styles.sectionLabel}>
            PENDING APPROVALS
            <Text style={styles.sectionCount}> · {pending.length}</Text>
          </Text>
        ) : null
      }
      ListEmptyComponent={
        <CenteredState
          glyph={<SymbolView accessibilityElementsHidden name="shield" size={28} tintColor={palette.fgMute} />}
          title="No pending approvals."
          detail="Approvals from your agents will show up here."
        />
      }
      renderItem={({ item }) => (
        <View style={styles.approvalItem}>
          <ApprovalCard
            approval={item}
            agents={state.agents}
            members={state.members}
            onApprove={approve}
            onReject={reject}
            onOpenIssue={(issueId) =>
              router.push({
                pathname: "/(app)/teams/issue/[id]",
                params: { id: issueId },
              })
            }
          />
        </View>
      )}
    />
  );
}

// ---------------------------------------------------------------------------
// Inbox — tabbed list (Mine / Recent / Unread / All, server-side filters) with
// search and a failed-runs band (web InboxToolbar + NeedsAttentionBand parity).
// ---------------------------------------------------------------------------

/** Copy for the tab-appropriate empty state. `mine` explains its real
 * semantics — touched-by-you, NOT assigned-to-you (the old copy was wrong). */
const INBOX_EMPTY_COPY: Record<InboxTab, { title: string; detail: string }> = {
  mine: {
    title: "Nothing here yet",
    detail:
      "Tasks you’ve touched show up here. Switch to All to see everything your team is working on.",
  },
  recent: { title: "No recent activity", detail: "Recently active tasks show up here." },
  unread: { title: "You’re all caught up", detail: "Unread updates show up here." },
  all: { title: "No tasks yet", detail: "Create one with the + button." },
};

function InboxTabChips({
  tab,
  onChange,
}: {
  tab: InboxTab;
  onChange: (tab: InboxTab) => void;
}) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <View style={styles.tabRow}>
      {INBOX_TABS.map((key) => {
        const active = tab === key;
        return (
          <Pressable
            key={key}
            style={[styles.tabChip, active && styles.tabChipActive]}
            onPress={() => onChange(key)}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
            accessibilityLabel={`${INBOX_TAB_LABELS[key]} tab`}
          >
            <Text style={[styles.tabChipText, active && styles.tabChipTextActive]}>
              {INBOX_TAB_LABELS[key]}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

/** One failed heartbeat run (web FailedRunRow parity): agent — reason, with
 * View / Retry / Dismiss. Dismiss is a local acknowledgement. */
function FailedRunRow({
  run,
  agents,
  onRetry,
  onDismiss,
}: {
  run: FailedRun;
  agents: TeamsAgent[];
  onRetry: (run: FailedRun) => Promise<void>;
  onDismiss: (runId: string) => void;
}) {
  const [retryPhase, setRetryPhase] = useState<"idle" | "retrying" | "done">("idle");
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const agentName = agents.find((a) => a.id === run.agentId)?.name ?? "Agent";
  const title = `${agentName} — ${run.failureReason || "run failed"}`;
  return (
    <View style={styles.failedRow}>
      <SymbolView accessibilityElementsHidden name="exclamationmark.triangle" size={14} tintColor={palette.alert} />
      <View style={styles.failedRowText}>
        <Text style={styles.failedRowTitle} numberOfLines={2}>
          {title}
        </Text>
        {run.startedAt ? (
          <Text style={styles.metaLine}>{timeAgo(run.startedAt)}</Text>
        ) : null}
      </View>
      <Pressable
        onPress={() =>
          router.push({ pathname: "/(app)/teams/run/[runId]", params: { runId: run.id } })
        }
        hitSlop={6}
        accessibilityRole="button"
        accessibilityLabel={`View run ${run.id}`}
      >
        <Text style={styles.failedRowAction}>View</Text>
      </Pressable>
      {run.agentId ? (
        <Pressable
          onPress={() => {
            if (retryPhase !== "idle") return;
            setRetryPhase("retrying");
            void onRetry(run).then(() => setRetryPhase("done"));
          }}
          hitSlop={6}
          disabled={retryPhase !== "idle"}
          accessibilityRole="button"
          accessibilityLabel="Retry run"
        >
          <Text
            style={[styles.failedRowAction, retryPhase !== "idle" && styles.failedRowActionMuted]}
          >
            {retryPhase === "retrying" ? "Retrying…" : retryPhase === "done" ? "Retried" : "Retry"}
          </Text>
        </Pressable>
      ) : null}
      <Pressable
        onPress={() => onDismiss(run.id)}
        hitSlop={6}
        accessibilityRole="button"
        accessibilityLabel="Dismiss failed run"
      >
        <Text style={styles.failedRowActionMuted}>Dismiss</Text>
      </Pressable>
    </View>
  );
}

function InboxView() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const [tab, setTab] = useState<InboxTab>("mine");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  // Debounce the server-side search so each keystroke doesn't fire a request.
  useEffect(() => {
    const timer = setTimeout(() => setSearch(searchInput), 350);
    return () => clearTimeout(timer);
  }, [searchInput]);
  const {
    state,
    refreshing,
    retry,
    refresh,
    dismissedRunIds,
    dismissFailedRun,
    retryFailedRun,
  } = useTeamsInbox(tab, search);

  const controls = (
    <View style={styles.inboxControls}>
      <InboxTabChips tab={tab} onChange={setTab} />
      <View style={styles.searchBox}>
        <SymbolView accessibilityElementsHidden name="magnifyingglass" size={14} tintColor={palette.fgMute} />
        <TextInput
          style={styles.searchInput}
          placeholder="Search tasks"
          placeholderTextColor={palette.fgMute}
          value={searchInput}
          onChangeText={setSearchInput}
          autoCapitalize="none"
          autoCorrect={false}
          returnKeyType="search"
          accessibilityLabel="Search tasks"
        />
        {searchInput !== "" && (
          <Pressable
            onPress={() => setSearchInput("")}
            hitSlop={8}
            accessibilityRole="button"
            accessibilityLabel="Clear search"
          >
            <SymbolView accessibilityElementsHidden name="xmark" size={14} tintColor={palette.fgMute} />
          </Pressable>
        )}
      </View>
    </View>
  );

  if (state.kind === "loading") {
    return (
      <View style={styles.page}>
        {controls}
        <SkeletonRows />
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
    // unavailable
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

  // ready
  const empty = state.items.length === 0;
  const failedRuns = state.failedRuns.filter((run) => !dismissedRunIds.has(run.id));
  const emptyCopy = search.trim()
    ? { title: "No matches", detail: `Nothing matches “${search.trim()}”.` }
    : INBOX_EMPTY_COPY[tab];
  return (
    <View style={styles.page}>
      {controls}
      <FlatList
        contentInsetAdjustmentBehavior="automatic"
        automaticallyAdjustKeyboardInsets
        style={styles.page}
        data={state.items}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[styles.insetListContent, empty && styles.grow]}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={refresh}
            tintColor={palette.fgMute}
          />
        }
        ListHeaderComponent={
          <>
            {failedRuns.length > 0 && (
              <View style={styles.failedBand}>
                <Text style={styles.failedBandLabel}>NEEDS ATTENTION</Text>
                {failedRuns.map((run) => (
                  <FailedRunRow
                    key={run.id}
                    run={run}
                    agents={state.agents}
                    onRetry={retryFailedRun}
                    onDismiss={dismissFailedRun}
                  />
                ))}
              </View>
            )}
            {!empty && (
              <Text style={[styles.sectionLabel, styles.sectionLabelInset]}>
                INBOX
                <Text style={styles.sectionCount}> · {state.items.length}</Text>
              </Text>
            )}
          </>
        }
        ListEmptyComponent={
          <CenteredState
            glyph={<LogoMark size={48} />}
            title={emptyCopy.title}
            detail={emptyCopy.detail}
          />
        }
        renderItem={({ item, index }) => (
          <InboxRow item={item} index={index} count={state.items.length} />
        )}
      />
    </View>
  );
}
