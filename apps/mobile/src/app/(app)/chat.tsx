import { router } from "expo-router";
import { type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import {
  agentDisplayName,
  useGatewayRpc,
  type AgentsListResponse,
} from "@isol8/shared";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { usePulse } from "../../components/chat/usePulse";
import { useGateway } from "../../providers/gateway";

// Slim connection-state row above the roster (web HealthIndicator, spec §5).
function HealthRow({
  isConnected,
  errorMessage,
}: {
  isConnected: boolean;
  errorMessage: string | null;
}) {
  const state = isConnected ? "connected" : errorMessage ? "down" : "connecting";
  const pulse = usePulse(state === "connecting", 0.35);
  const dotColor =
    state === "connected"
      ? colors.runningGreen
      : state === "down"
        ? colors.danger
        : colors.connectingYellow;
  const label =
    state === "connected"
      ? "Connected"
      : state === "down"
        ? (errorMessage ?? "Disconnected")
        : "Connecting…";
  return (
    <View style={styles.healthRow}>
      <Animated.View
        style={[styles.healthDot, { backgroundColor: dotColor, opacity: pulse }]}
      />
      <Text
        style={[styles.healthLabel, state === "down" && styles.healthLabelDown]}
        numberOfLines={1}
      >
        {label}
      </Text>
    </View>
  );
}

// 28px tinted avatar tile — Lora italic-bold initial, or the agent's emoji
// inside the same tile when one is set (spec §5 Monogram).
function Monogram({ name, emoji }: { name: string; emoji?: string }) {
  return (
    <View style={styles.monogram}>
      {emoji ? (
        <Text style={styles.monogramEmoji}>{emoji}</Text>
      ) : (
        <Text style={styles.monogramLetter}>
          {(name.trim()[0] ?? "8").toUpperCase()}
        </Text>
      )}
    </View>
  );
}

// Static "active" status dot with soft halo (spec §5 StatusDot).
function StatusDot() {
  return (
    <View style={styles.statusHalo}>
      <View style={styles.statusDot} />
    </View>
  );
}

// Three pulsing placeholder rows while agents.list is in flight (spec §7).
function SkeletonRows() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

// Agent roster — entry point to per-agent chat. Streams over the same
// agents.list RPC the web sidebar uses.
export default function Chat() {
  const gateway = useGateway();
  const { data, error, isLoading } = useGatewayRpc<AgentsListResponse>(
    gateway,
    "agents.list",
  );
  const agents = data?.agents ?? [];
  const connectionDown = !gateway.isConnected && gateway.error != null;
  const showEmpty = agents.length === 0 && !isLoading && data != null;

  let body: ReactElement;
  if (error) {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load agents: {error.message}
        </Text>
      </View>
    );
  } else if (!data && connectionDown) {
    // No cached roster and the socket is down — the health row carries the
    // detail; keep the body quiet rather than pulsing skeletons forever.
    body = (
      <View style={styles.centerGrow}>
        <Text style={styles.dim}>{gateway.error}</Text>
      </View>
    );
  } else {
    body = (
      <FlatList
        data={agents}
        keyExtractor={(a) => a.id}
        style={styles.list}
        contentContainerStyle={[
          styles.listContent,
          showEmpty && styles.centerGrow,
        ]}
        ListHeaderComponent={
          agents.length > 0 ? (
            <Text style={styles.sectionLabel}>
              AGENTS
              <Text style={styles.sectionCount}> · {agents.length}</Text>
            </Text>
          ) : null
        }
        ListEmptyComponent={
          isLoading || !data ? (
            <SkeletonRows />
          ) : (
            <View style={styles.emptyWrap}>
              <Text style={styles.emptyGlyph}>8</Text>
              <Text style={styles.dim}>No agents yet.</Text>
            </View>
          )
        }
        renderItem={({ item }) => (
          <Pressable
            style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
            onPress={() =>
              router.push({
                pathname: "/(app)/agent/[agentId]",
                params: { agentId: item.id, name: agentDisplayName(item) },
              })
            }
          >
            <Monogram
              name={agentDisplayName(item)}
              emoji={item.identity?.emoji ?? undefined}
            />
            <View style={styles.rowText}>
              <Text style={styles.name} numberOfLines={1}>
                {agentDisplayName(item)}
              </Text>
              <Text style={styles.id} numberOfLines={1}>
                {item.id}
              </Text>
            </View>
            <StatusDot />
          </Pressable>
        )}
      />
    );
  }

  return (
    <View style={styles.page}>
      <HealthRow isConnected={gateway.isConnected} errorMessage={gateway.error} />
      {body}
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  list: { flex: 1 },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingBottom: spacing.xl,
  },
  centerGrow: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },

  // Connection state (spec §5 HealthIndicator).
  healthRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  healthDot: { width: 8, height: 8, borderRadius: 4 },
  healthLabel: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    flexShrink: 1,
  },
  healthLabelDown: { color: colors.danger },

  // Section label (spec §5).
  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Roster rows (spec §5).
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: spacing.sm,
    borderRadius: 10,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  monogram: {
    width: 28,
    height: 28,
    borderRadius: radii.md,
    backgroundColor: colors.forestTint,
    alignItems: "center",
    justifyContent: "center",
  },
  monogramLetter: {
    fontFamily: fonts.loraBoldItalic,
    fontSize: 14,
    color: colors.forest,
  },
  monogramEmoji: { fontSize: 14 },
  rowText: { flex: 1 },
  name: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  id: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  statusHalo: {
    width: 13,
    height: 13,
    borderRadius: 7,
    backgroundColor: colors.forestHalo,
    alignItems: "center",
    justifyContent: "center",
  },
  statusDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: colors.forestSoft,
  },

  // Empty state (spec §7).
  emptyWrap: { alignItems: "center", gap: spacing.sm },
  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // Loading skeletons (spec §7).
  skeletonGroup: { alignSelf: "stretch", gap: spacing.sm },
  skeletonRow: {
    height: 44,
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
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
});
