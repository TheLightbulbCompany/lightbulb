import { router, useLocalSearchParams } from "expo-router";
import { useEffect, useRef } from "react";
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
  // Through a ref, and keyed on agentId alone: selectAgentDm's identity changes
  // with the room directory, and a second call supersedes the first, which then
  // resolves null — a re-fire would race its own fallback.
  const selectAgentDmRef = useRef(rooms.selectAgentDm);
  selectAgentDmRef.current = rooms.selectAgentDm;
  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    const giveUp = () => {
      if (!cancelled) router.replace("/(app)/home");
    };
    void selectAgentDmRef
      .current(agentId)
      .then((roomId) => {
        if (cancelled) return;
        // null = a newer selection superseded this one. Navigating somewhere is
        // the only exit; an unresolved spinner is forever.
        if (roomId) router.replace(`/(app)/room/${roomId}`);
        else giveUp();
      })
      .catch(giveUp);
    return () => {
      cancelled = true;
    };
  }, [agentId]);
  return (
    <View style={styles.wrap}>
      <ActivityIndicator color={colors.forest} />
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: colors.cream },
});
