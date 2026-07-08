import Feather from "@expo/vector-icons/Feather";
import { Stack, router } from "expo-router";
import { useState, type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { ApprovalCard } from "../../../components/teams/ApprovalCard";
import { BoardView } from "../../../components/teams/BoardView";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import {
  formatCents,
  humanizeStatus,
  isApprovalActionable,
  runDayTotal,
  timeAgo,
  totalPendingApprovals,
  useTeamsApprovals,
  useTeamsDashboard,
  useTeamsInbox,
  type InboxItem,
  type RunActivityDay,
} from "../../../lib/teamsData";

// Teams is a subscription-gated, web-managed surface: when the workspace needs
// a subscription the mobile app deep-links out rather than running checkout.
const MANAGE_URL = "https://isol8.co/teams";

// ---------------------------------------------------------------------------
// Readiness affordance (spec §7) — pulsing dot for the provisioning state.
// ---------------------------------------------------------------------------

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

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

// ---------------------------------------------------------------------------
// Inbox row (spec §5) — title / author+time / status preview + unread dot.
// ---------------------------------------------------------------------------

function InboxRow({ item }: { item: InboxItem }) {
  const identifier = item.identifier ?? item.id.slice(0, 8);
  const when = timeAgo(item.updatedAt ?? item.lastActivityAt ?? item.createdAt);
  const meta = [identifier, when].filter(Boolean).join(" · ");
  const preview = item.status ? humanizeStatus(item.status) : null;

  return (
    <Pressable
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
      onPress={() =>
        router.push({
          pathname: "/(app)/teams/issue/[id]",
          params: { id: item.id, title: item.title },
        })
      }
      accessibilityRole="button"
      accessibilityLabel={`${item.title}. ${meta}`}
    >
      <View style={styles.rowText}>
        <View style={styles.titleRow}>
          <Text style={styles.title} numberOfLines={1}>
            {item.title}
          </Text>
          {item.unread ? <View style={styles.unreadDot} /> : null}
        </View>
        {meta !== "" && (
          <Text style={styles.metaLine} numberOfLines={1}>
            {meta}
          </Text>
        )}
        {preview != null && (
          <Text style={styles.previewLine} numberOfLines={1}>
            {preview}
          </Text>
        )}
      </View>
      <Feather name="chevron-right" size={18} color={colors.muteSoft} />
    </Pressable>
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
// Mode toggle — Inbox (flat assigned list) vs Board (status-grouped, movable).
// ---------------------------------------------------------------------------

type TeamsMode = "overview" | "inbox" | "board" | "approvals";

function SegmentedToggle({
  mode,
  onChange,
}: {
  mode: TeamsMode;
  onChange: (mode: TeamsMode) => void;
}) {
  const segments: { key: TeamsMode; label: string }[] = [
    { key: "overview", label: "Overview" },
    { key: "inbox", label: "Inbox" },
    { key: "board", label: "Board" },
    { key: "approvals", label: "Approvals" },
  ];
  return (
    <View style={styles.toggle}>
      {segments.map((segment) => {
        const active = mode === segment.key;
        return (
          <Pressable
            key={segment.key}
            style={[styles.segment, active && styles.segmentActive]}
            onPress={() => onChange(segment.key)}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
            accessibilityLabel={`${segment.label} view`}
          >
            <Text
              style={[styles.segmentText, active && styles.segmentTextActive]}
              numberOfLines={1}
            >
              {segment.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen — hosts the Inbox / Board toggle and the two modes.
// ---------------------------------------------------------------------------

export default function TeamsScreen() {
  const [mode, setMode] = useState<TeamsMode>("overview");
  // Create-issue sheet visibility lives here (not in BoardView) so the header
  // "+" is driven by `mode` — it appears in board mode and is explicitly
  // cleared (headerRight: undefined) in inbox mode, so switching tabs never
  // leaves a stale button.
  const [createOpen, setCreateOpen] = useState(false);
  return (
    <View style={styles.host}>
      <Stack.Screen
        options={{
          headerRight:
            mode === "board"
              ? () => (
                  <Pressable
                    onPress={() => setCreateOpen(true)}
                    hitSlop={10}
                    style={({ pressed }) => [
                      styles.headerBtn,
                      pressed && styles.headerBtnPressed,
                    ]}
                    accessibilityRole="button"
                    accessibilityLabel="New issue"
                  >
                    <Feather name="plus" size={22} color={colors.forest} />
                  </Pressable>
                )
              : undefined,
        }}
      />
      <View style={styles.toggleBar}>
        <SegmentedToggle mode={mode} onChange={setMode} />
      </View>
      {mode === "overview" ? (
        <DashboardView onOpenApprovals={() => setMode("approvals")} />
      ) : mode === "inbox" ? (
        <InboxView />
      ) : mode === "approvals" ? (
        <ApprovalsView />
      ) : (
        <BoardView createOpen={createOpen} onCloseCreate={() => setCreateOpen(false)} />
      )}
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
                  backgroundColor:
                    total > 0 ? colors.forestSoft : colors.creamDeepest,
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
            glyph={<Feather name="lock" size={28} color={colors.mute} />}
            title="Teams requires an active subscription"
            detail="Manage your subscription on isol8.co to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on isol8.co"
                onPress={() => void Linking.openURL(MANAGE_URL)}
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
      style={styles.page}
      contentContainerStyle={styles.dashContent}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={refresh}
          tintColor={colors.mute}
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
    </ScrollView>
  );
}

// ---------------------------------------------------------------------------
// Approvals view (contract §2) — global inbox of pending, actionable approvals
// with inline Approve / Reject. Same 202/402/503 readiness gate as the inbox.
// ---------------------------------------------------------------------------

function ApprovalsView() {
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
            glyph={<Feather name="lock" size={28} color={colors.mute} />}
            title="Teams requires an active subscription"
            detail="Manage your subscription on isol8.co to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on isol8.co"
                onPress={() => void Linking.openURL(MANAGE_URL)}
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

  // ready — the global inbox shows only actionable (pending / revision) rows.
  const pending = state.approvals.filter(isApprovalActionable);
  const empty = pending.length === 0;
  return (
    <FlatList
      style={styles.page}
      data={pending}
      keyExtractor={(item) => item.id}
      contentContainerStyle={[styles.listContent, empty && styles.grow]}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={refresh}
          tintColor={colors.mute}
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
          glyph={<Feather name="shield" size={28} color={colors.mute} />}
          title="No pending approvals."
          detail="Approval requests from your agents will show up here."
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

function InboxView() {
  const { state, refreshing, retry, refresh } = useTeamsInbox();

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
            glyph={<Feather name="lock" size={28} color={colors.mute} />}
            title="Teams requires an active subscription"
            detail="Manage your subscription on isol8.co to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on isol8.co"
                onPress={() => void Linking.openURL(MANAGE_URL)}
              />
            }
          />
        </View>
      );
    }
    // unavailable
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

  // ready
  const empty = state.items.length === 0;
  return (
    <FlatList
      style={styles.page}
      data={state.items}
      keyExtractor={(item) => item.id}
      contentContainerStyle={[styles.listContent, empty && styles.grow]}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={refresh}
          tintColor={colors.mute}
        />
      }
      ListHeaderComponent={
        !empty ? (
          <Text style={styles.sectionLabel}>
            INBOX
            <Text style={styles.sectionCount}> · {state.items.length}</Text>
          </Text>
        ) : null
      }
      ListEmptyComponent={
        <CenteredState
          glyph={<Text style={styles.emptyGlyph}>8</Text>}
          title="No items"
          detail="Issues assigned to you will show up here."
        />
      }
      renderItem={({ item }) => <InboxRow item={item} />}
    />
  );
}

const styles = StyleSheet.create({
  host: { flex: 1, backgroundColor: colors.cream },

  // Header "+" (board mode only).
  headerBtn: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
    borderRadius: radii.sm,
  },
  headerBtnPressed: { opacity: 0.55 },

  // Inbox / Board segmented toggle.
  toggleBar: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.sm,
    paddingBottom: spacing.sm,
    backgroundColor: colors.cream,
  },
  toggle: {
    flexDirection: "row",
    alignSelf: "stretch",
    backgroundColor: colors.creamDeeper,
    borderRadius: radii.md,
    padding: spacing.xs,
    gap: spacing.xs,
  },
  segment: {
    flex: 1,
    alignItems: "center",
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
  },
  segmentActive: {
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.edge,
  },
  segmentText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
  segmentTextActive: { color: colors.ink },

  page: { flex: 1, backgroundColor: colors.cream },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
  },
  grow: { flexGrow: 1, justifyContent: "center" },

  // Section label (spec §5).
  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Approvals list — spacing between approval cards.
  approvalItem: { marginBottom: spacing.md },

  // Inbox rows (spec §5).
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.sm,
    borderRadius: radii.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowText: { flex: 1, gap: 3 },
  titleRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  title: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    flexShrink: 1,
  },
  unreadDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: colors.forest,
  },
  metaLine: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  previewLine: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
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
  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // Buttons.
  button: {
    marginTop: spacing.xs,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  buttonDanger: { borderColor: colors.errorBorder },
  buttonPressed: { opacity: 0.6 },
  buttonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  buttonTextDanger: { color: colors.errorText },

  // Loading skeletons (spec §7).
  skeletonGroup: {
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
  },
  skeletonRow: {
    height: 56,
    borderRadius: radii.md,
    backgroundColor: colors.skeleton,
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
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  dashTilePressed: { opacity: 0.6 },
  dashTileLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
  },
  dashTileValue: { ...type.title },
  dashSkeletonLabel: {
    height: 10,
    width: "60%",
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },
  dashSkeletonValue: {
    height: 22,
    width: "45%",
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },

  // Run-activity summary card (lean bar row; NO chart library).
  activityCard: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.md,
  },
  activityHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  activityTotal: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
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
