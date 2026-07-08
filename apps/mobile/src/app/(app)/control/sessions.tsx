import { useCallback, useMemo, useState, type ReactElement } from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import {
  agentDisplayName,
  useGatewayRpc,
  useGatewayRpcMutation,
  type Agent,
  type AgentsListResponse,
} from "@isol8/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { relativeTime } from "../../../lib/agentSessions";
import { useGateway } from "../../../providers/gateway";

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §4 Sessions)
//
// Global session list across agents: sessions.list with includeGlobal +
// includeUnknown so the panel surfaces every session, not just one agent's.
// sessions.list may arrive bare (Session[]) or wrapped ({sessions:[…]}); the
// per-row `model`/`lastMessage` shape varies by OpenClaw version, so every
// field is optional and read defensively.
// ---------------------------------------------------------------------------

const SESSIONS_LIST_PARAMS = {
  includeGlobal: true,
  includeUnknown: true,
  includeDerivedTitles: true,
  includeLastMessage: true,
  limit: 100,
} as const;

interface RawSession {
  key?: string;
  kind?: string;
  agentId?: string;
  label?: string;
  displayName?: string;
  derivedTitle?: string;
  title?: string;
  updatedAt?: number;
  totalTokens?: number;
  contextTokens?: number;
  lastMessage?: { text?: string };
}

interface GlobalSession {
  key: string;
  label: string;
  agentId: string | null;
  kind: string | null;
  updatedAt?: number;
  preview?: string;
  totalTokens?: number;
  contextTokens?: number;
}

function normalizeSessions(payload: unknown): RawSession[] {
  if (Array.isArray(payload)) return payload as RawSession[];
  const wrapped = (payload as { sessions?: RawSession[] } | null)?.sessions;
  return Array.isArray(wrapped) ? wrapped : [];
}

/** agentId lives on the row, else parse it out of `agent:<id>:<user>[:uuid]`. */
function agentIdFor(row: RawSession, key: string): string | null {
  if (row.agentId) return row.agentId;
  const parts = key.split(":");
  if (parts[0] === "agent" && parts[1]) return parts[1];
  return null;
}

function labelFor(row: RawSession, key: string): string {
  const raw = (
    row.label ||
    row.displayName ||
    row.derivedTitle ||
    row.title ||
    row.lastMessage?.text ||
    ""
  ).trim();
  const fallback = raw || `Session ${key.split(":").pop()?.slice(0, 8) ?? key}`;
  return fallback.length > 56 ? `${fallback.slice(0, 56)}…` : fallback;
}

function projectGlobalSessions(payload: unknown): GlobalSession[] {
  const rows = normalizeSessions(payload);
  const out: GlobalSession[] = [];
  for (const row of rows) {
    if (typeof row?.key !== "string") continue;
    out.push({
      key: row.key,
      label: labelFor(row, row.key),
      agentId: agentIdFor(row, row.key),
      kind: typeof row.kind === "string" ? row.kind : null,
      updatedAt: row.updatedAt,
      preview: row.lastMessage?.text?.trim() || undefined,
      totalTokens: row.totalTokens,
      contextTokens: row.contextTokens,
    });
  }
  out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
  return out;
}

const KIND_LABEL: Record<string, string> = {
  direct: "Direct",
  group: "Group",
  global: "Global",
  unknown: "Unknown",
};

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(n);
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

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function SessionsPanel() {
  const gateway = useGateway();
  const callRpc = useGatewayRpcMutation(gateway);

  const { data, error, isLoading, mutate } = useGatewayRpc<unknown>(
    gateway,
    "sessions.list",
    SESSIONS_LIST_PARAMS,
  );
  const { data: agentsData } = useGatewayRpc<AgentsListResponse>(
    gateway,
    "agents.list",
  );

  const agentNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const a of agentsData?.agents ?? []) {
      map.set(a.id.toLowerCase(), agentDisplayName(a as Agent));
    }
    return map;
  }, [agentsData]);

  const sessions = useMemo(() => projectGlobalSessions(data), [data]);

  const [refreshing, setRefreshing] = useState(false);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await mutate();
    } finally {
      setRefreshing(false);
    }
  }, [mutate]);

  const confirmDelete = useCallback(
    (session: GlobalSession) => {
      Alert.alert(
        "Delete session?",
        `“${session.label}” and its transcript will be removed. This cannot be undone.`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Delete",
            style: "destructive",
            onPress: () => {
              setDeletingKey(session.key);
              void callRpc("sessions.delete", {
                key: session.key,
                deleteTranscript: true,
              })
                .then(() => mutate())
                .catch((err: unknown) => {
                  Alert.alert(
                    "Couldn’t delete session",
                    err instanceof Error ? err.message : String(err),
                  );
                })
                .finally(() => setDeletingKey(null));
            },
          },
        ],
      );
    },
    [callRpc, mutate],
  );

  const connectionDown = !gateway.isConnected && gateway.error != null;
  const showEmpty = sessions.length === 0 && !isLoading && data != null;

  const renderItem = useCallback(
    ({ item }: { item: GlobalSession }) => {
      const isDeleting = deletingKey === item.key;
      const agentLabel = item.agentId
        ? agentNames.get(item.agentId.toLowerCase()) ?? item.agentId
        : item.kind
          ? KIND_LABEL[item.kind] ?? item.kind
          : "—";
      const kindLabel = item.kind ? KIND_LABEL[item.kind] ?? item.kind : null;
      const tokens = item.totalTokens != null ? formatTokens(item.totalTokens) : null;
      return (
        <Pressable
          style={({ pressed }) => [
            styles.row,
            pressed && styles.rowPressed,
            isDeleting && styles.rowDeleting,
          ]}
          onLongPress={() => confirmDelete(item)}
          disabled={isDeleting}
          accessibilityRole="button"
          accessibilityLabel={`${item.label}. Long-press to delete.`}
        >
          <View style={styles.rowText}>
            <View style={styles.rowTop}>
              <Text style={styles.label} numberOfLines={1}>
                {item.label}
              </Text>
              {item.updatedAt ? (
                <Text style={styles.time}>{relativeTime(item.updatedAt)}</Text>
              ) : null}
            </View>
            <View style={styles.metaRow}>
              <Text style={styles.agent} numberOfLines={1}>
                {agentLabel}
              </Text>
              {kindLabel != null && (
                <View style={styles.kindBadge}>
                  <Text style={styles.kindBadgeText}>{kindLabel}</Text>
                </View>
              )}
              {tokens != null && (
                <Text style={styles.tokens}>{tokens} tok</Text>
              )}
            </View>
            {item.preview ? (
              <Text style={styles.preview} numberOfLines={1}>
                {item.preview}
              </Text>
            ) : null}
          </View>
          {isDeleting ? (
            <ActivityIndicator size="small" color={colors.mute} />
          ) : null}
        </Pressable>
      );
    },
    [agentNames, confirmDelete, deletingKey],
  );

  let body: ReactElement;
  if (error) {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load sessions: {error.message}
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
  } else if (!data && connectionDown) {
    body = (
      <View style={styles.centerGrow}>
        <Text style={styles.dim}>{gateway.error}</Text>
      </View>
    );
  } else {
    body = (
      <FlatList
        data={sessions}
        keyExtractor={(s) => s.key}
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
        ListHeaderComponent={
          sessions.length > 0 ? (
            <Text style={styles.sectionLabel}>
              SESSIONS
              <Text style={styles.sectionCount}> · {sessions.length}</Text>
            </Text>
          ) : null
        }
        ListEmptyComponent={
          isLoading || !data ? (
            <SkeletonRows />
          ) : (
            <View style={styles.emptyWrap}>
              <Text style={styles.emptyGlyph}>8</Text>
              <Text style={styles.dim}>No sessions yet.</Text>
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
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Session rows.
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    padding: spacing.sm,
    borderRadius: 10,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowDeleting: { opacity: 0.5 },
  rowText: { flex: 1, gap: 2 },
  rowTop: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: spacing.sm,
  },
  label: {
    flexShrink: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  time: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 14,
    color: colors.mute,
  },
  metaRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  agent: {
    flexShrink: 1,
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  kindBadge: {
    backgroundColor: colors.chip,
    borderRadius: radii.xs,
    paddingHorizontal: spacing.xs,
    paddingVertical: 1,
  },
  kindBadgeText: {
    fontFamily: fonts.mono,
    fontSize: 9,
    lineHeight: 12,
    letterSpacing: 0.6,
    color: colors.mute,
  },
  tokens: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    color: colors.muteSoft,
  },
  preview: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  // Empty state (spec §7).
  emptyWrap: { alignItems: "center", gap: spacing.sm },
  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // Skeletons (spec §7).
  skeletonGroup: { alignSelf: "stretch", gap: spacing.sm },
  skeletonRow: {
    height: 52,
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
    fontFamily: fonts.dmSans,
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
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
});
