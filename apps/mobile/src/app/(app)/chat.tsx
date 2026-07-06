import { router } from "expo-router";
import { FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import {
  agentDisplayName,
  useGatewayRpc,
  type AgentsListResponse,
} from "@isol8/shared";

import { colors, fonts, spacing, type } from "../../config/theme";
import { useGateway } from "../../providers/gateway";

// Agent roster — entry point to per-agent chat. Streams over the same
// agents.list RPC the web sidebar uses.
export default function Chat() {
  const gateway = useGateway();
  const { data, error, isLoading } = useGatewayRpc<AgentsListResponse>(
    gateway,
    "agents.list",
  );
  const agents = data?.agents ?? [];

  if (!gateway.isConnected && !data) {
    return (
      <View style={styles.center}>
        <Text style={styles.dim}>
          {gateway.error ?? "Connecting to your workspace…"}
        </Text>
      </View>
    );
  }

  if (error) {
    return (
      <View style={styles.center}>
        <Text style={styles.dim}>Couldn’t load agents: {error.message}</Text>
      </View>
    );
  }

  return (
    <FlatList
      data={agents}
      keyExtractor={(a) => a.id}
      contentContainerStyle={agents.length === 0 ? styles.centerGrow : undefined}
      ListEmptyComponent={
        <Text style={styles.dim}>
          {isLoading ? "Loading agents…" : "No agents yet."}
        </Text>
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
          <Text style={styles.emoji}>{item.identity?.emoji ?? "🤖"}</Text>
          <View style={styles.rowText}>
            <Text style={styles.name}>{agentDisplayName(item)}</Text>
            <Text style={styles.id}>{item.id}</Text>
          </View>
        </Pressable>
      )}
    />
  );
}

const styles = StyleSheet.create({
  center: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  centerGrow: { flexGrow: 1, alignItems: "center", justifyContent: "center" },
  dim: { ...type.body, color: colors.muted, textAlign: "center" },
  // White hairline rows on the cream page, matching the web sidebar list.
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    backgroundColor: colors.white,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  rowPressed: { backgroundColor: colors.creamHover },
  emoji: { fontFamily: fonts.dmSans, fontSize: 24 },
  rowText: { flex: 1 },
  name: { ...type.body, fontFamily: fonts.dmSansMedium },
  id: { ...type.caption },
});
