import { FlatList, Pressable, StyleSheet, Text, View } from "react-native";
import { agentDisplayName, type AgentsListResponse } from "@isol8/shared";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

type Agent = NonNullable<AgentsListResponse["agents"]>[number];

interface Props {
  visible: boolean;
  onClose: () => void;
  agents: Agent[];
  onPick: (agent: Agent) => void;
}

/**
 * Slide-up agent picker for the roster's "New task" action: pick an agent to
 * start a brand-new session with. Shares the cream-card / drag-handle shell
 * with SessionsSheet; the caller owns navigation + session minting.
 */
export function AgentPickerSheet({ visible, onClose, agents, onPick }: Props) {
  return (
    <BottomSheet
      visible={visible}
      onClose={onClose}
      closeLabel="Close agent picker"
      maxHeight="70%"
    >
      <View style={styles.card}>
        <View style={styles.handle} />
        <Text style={styles.overline}>START A NEW TASK WITH…</Text>
        <FlatList
          data={agents}
          keyExtractor={(a) => a.id}
          contentContainerStyle={styles.list}
          renderItem={({ item }) => (
            <Pressable
              style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
              onPress={() => onPick(item)}
              accessibilityRole="button"
              accessibilityLabel={agentDisplayName(item)}
            >
              <View style={styles.monogram}>
                {item.identity?.emoji ? (
                  <Text style={styles.emoji}>{item.identity.emoji}</Text>
                ) : (
                  <Text style={styles.letter}>
                    {(agentDisplayName(item).trim()[0] ?? "8").toUpperCase()}
                  </Text>
                )}
              </View>
              <Text style={styles.name} numberOfLines={1}>
                {agentDisplayName(item)}
              </Text>
            </Pressable>
          )}
        />
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.cream,
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    paddingBottom: spacing.xl,
  },
  handle: {
    alignSelf: "center",
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.edge,
    marginTop: spacing.sm,
  },
  overline: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.lg,
    paddingBottom: spacing.sm,
  },
  list: { paddingHorizontal: spacing.sm, paddingBottom: spacing.sm },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: spacing.sm,
    borderRadius: radii.md,
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
  letter: { fontFamily: fonts.loraBoldItalic, fontSize: 14, color: colors.forest },
  emoji: { fontSize: 14 },
  name: {
    flex: 1,
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.ink,
  },
});
