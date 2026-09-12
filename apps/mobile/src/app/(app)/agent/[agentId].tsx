import { router, useLocalSearchParams } from "expo-router";
import { useEffect } from "react";
import { ActivityIndicator, StyleSheet, View } from "react-native";

import { colors } from "../../../config/theme";
import { useMobileRooms } from "../../../lib/rooms";

/**
 * Legacy route. Old push payloads and `flickmeon://agent/<id>` deep links
 * still name an agent, not a room, so this resolves (or creates) that agent's
 * canonical DM and replaces itself with the room route.
 */
export default function AgentRedirect() {
  const { agentId } = useLocalSearchParams<{ agentId: string }>();
  const rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  const { selectAgentDm } = rooms;
  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    void selectAgentDm(agentId)
      .then((roomId) => {
        if (!cancelled && roomId) router.replace(`/(app)/room/${roomId}`);
      })
      .catch(() => {
        if (!cancelled) router.replace("/(app)/home");
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, selectAgentDm]);
  return (
    <View style={styles.wrap}>
      <ActivityIndicator color={colors.forest} />
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: colors.cream },
});
