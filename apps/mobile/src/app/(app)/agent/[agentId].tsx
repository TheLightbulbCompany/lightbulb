import { router, useLocalSearchParams } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts } from "../../../config/theme";
import { useMobileRooms } from "../../../lib/rooms";

/**
 * Legacy route. Old push payloads and `flickmeon://agent/<id>` deep links
 * still name an agent, not a room, so this resolves (or creates) that agent's
 * canonical DM and replaces itself with the room route.
 */
export default function AgentRedirect() {
  const { agentId } = useLocalSearchParams<{ agentId: string }>();
  const rooms = useMobileRooms({ autoSelect: false, subscribeToRoom: false });
  // Retry nonce: re-fires the resolve below without remounting.
  const [attempt, setAttempt] = useState(0);
  // Through a ref, and keyed on agentId alone: selectAgentDm's identity changes
  // with the room directory, and a second call supersedes the first, which then
  // resolves null — a re-fire would race its own fallback.
  const selectAgentDmRef = useRef(rooms.selectAgentDm);
  selectAgentDmRef.current = rooms.selectAgentDm;
  useEffect(() => {
    if (!agentId) return;
    let cancelled = false;
    const giveUp = () => {
      if (!cancelled) router.replace("/(app)/(tabs)/chats");
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
      // No redirect on failure: the error card below (rooms.selectRoomError)
      // is the surface, with a retry. The old silent bounce to home hid
      // every resolve-or-create failure.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [agentId, attempt]);
  if (rooms.selectRoomError) {
    return (
      <View style={styles.wrap}>
        <Text style={styles.errorText}>{rooms.selectRoomError}</Text>
        <Pressable
          onPress={() => setAttempt((count) => count + 1)}
          accessibilityRole="button"
          accessibilityLabel="Retry opening chat"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  }
  return (
    <View style={styles.wrap}>
      <ActivityIndicator color={colors.forest} />
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: colors.cream, gap: 8, padding: 24 },
  errorText: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.errorText, textAlign: "center" },
  retryText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
});
