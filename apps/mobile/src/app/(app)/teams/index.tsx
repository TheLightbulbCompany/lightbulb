import Feather from "@expo/vector-icons/Feather";
import { Stack, router } from "expo-router";
import { useEffect, useState, type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { ApprovalCard } from "../../../components/teams/ApprovalCard";
import { BoardView } from "../../../components/teams/BoardView";
import { CreateIssueSheet } from "../../../components/teams/CreateIssueSheet";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
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
import { teamsPanelUrl } from "../../../lib/webLinks";
import { LogoMark } from "../../../components/LogoMark";

// Teams is a subscription-gated, web-managed surface: when the workspace needs
// a subscription the mobile app deep-links out rather than running checkout.

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

  return (
    <View style={styles.host}>
      <Stack.Screen
        options={{
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
              <Feather name="plus" size={22} color={colors.forest} />
            </Pressable>
          ),
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
            detail="Manage your subscription on flickmeon.ai to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on flickmeon.ai"
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
      <Pressable
        style={({ pressed }) => [styles.linkRow, pressed && styles.rowPressed]}
        onPress={() => router.push("/(app)/teams/routines")}
        accessibilityRole="button"
        accessibilityLabel="Routines"
      >
        <Feather name="repeat" size={16} color={colors.forest} />
        <Text style={styles.linkRowText}>Routines</Text>
        <Feather name="chevron-right" size={16} color={colors.muteSoft} />
      </Pressable>
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
            detail="Manage your subscription on flickmeon.ai to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on flickmeon.ai"
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
  const agentName = agents.find((a) => a.id === run.agentId)?.name ?? "Agent";
  const title = `${agentName} — ${run.failureReason || "run failed"}`;
  return (
    <View style={styles.failedRow}>
      <Feather name="alert-triangle" size={14} color={colors.danger} />
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
        <Feather name="search" size={14} color={colors.mute} />
        <TextInput
          style={styles.searchInput}
          placeholder="Search tasks"
          placeholderTextColor={colors.placeholder}
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
            <Feather name="x" size={14} color={colors.mute} />
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
            glyph={<Feather name="lock" size={28} color={colors.mute} />}
            title="Teams requires an active subscription"
            detail="Manage your subscription on flickmeon.ai to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on flickmeon.ai"
                onPress={() => void Linking.openURL(teamsPanelUrl("issues"))}
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
  const failedRuns = state.failedRuns.filter((run) => !dismissedRunIds.has(run.id));
  const emptyCopy = search.trim()
    ? { title: "No matches", detail: `Nothing matches “${search.trim()}”.` }
    : INBOX_EMPTY_COPY[tab];
  return (
    <View style={styles.page}>
      {controls}
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
              <Text style={styles.sectionLabel}>
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
        renderItem={({ item }) => <InboxRow item={item} />}
      />
    </View>
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
    fontFamily: fonts.sansMedium,
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
    backgroundColor: colors.creamDeeper,
  },
  tabChipActive: {
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.edge,
  },
  tabChipText: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  tabChipTextActive: { color: colors.ink },
  searchBox: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.creamDeeper,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
  },
  searchInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    padding: 0,
  },

  // Failed-runs band (web NeedsAttentionBand parity).
  failedBand: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.md,
    marginBottom: spacing.md,
    gap: spacing.sm,
  },
  failedBandLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.danger,
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
    color: colors.ink,
  },
  failedRowAction: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.forest,
  },
  failedRowActionMuted: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  // Overview → Routines link row.
  linkRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
  },
  linkRowText: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 19,
    color: colors.ink,
  },

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
    fontFamily: fonts.sansMedium,
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
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  previewLine: {
    fontFamily: fonts.sans,
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
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    textAlign: "center",
  },
  stateDetail: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 19,
    color: colors.mute,
    textAlign: "center",
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
    fontFamily: fonts.sansMedium,
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
    fontFamily: fonts.sansMedium,
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
