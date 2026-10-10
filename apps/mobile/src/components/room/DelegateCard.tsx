import { subagentActivity, type MessageDelegations, type RoomSubagents, type SubagentStatus } from "@lightbulb/shared";
import { StyleSheet, Text, View } from "react-native";

import { fonts, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { AVATAR_GUTTER } from "../conversation/bubbles";

const ICON: Record<SubagentStatus | "dispatched", { glyph: string; label: string; tone: keyof ChatPalette }> = {
  running: { glyph: "…", label: "Running", tone: "fgMute" },
  queued: { glyph: "○", label: "Queued", tone: "fgMute" },
  completed: { glyph: "✓", label: "Completed", tone: "live" },
  failed: { glyph: "✗", label: "Failed", tone: "alert" },
  interrupted: { glyph: "■", label: "Stopped", tone: "warn" },
  dispatched: { glyph: "•", label: "Dispatched", tone: "fgMute" },
};

/** One row per child of this fan-out; mirrors web's DelegateCard. */
export function DelegateCard({ state, agentId, delegationId, agentLive }: {
  state: RoomSubagents; agentId: string; delegationId: string; agentLive: boolean;
}) {
  const palette = useChatPalette();
  const children = state.items.filter((i) => i.agentId === agentId && i.delegationId === delegationId)
    .sort((a, b) => a.taskIndex - b.taskIndex);
  const row = (key: string, icon: (typeof ICON)[keyof typeof ICON], goal: string, meta?: string, activity?: string) => (
    <View key={key} style={styles.row}>
      <Text accessible accessibilityLabel={icon.label} style={[styles.icon, { color: palette[icon.tone] }]}>{icon.glyph}</Text>
      <View style={styles.text}>
        <Text style={[styles.goal, { color: palette.fg }]} maxFontSizeMultiplier={1.3}>{goal}</Text>
        {meta ? <Text style={[styles.meta, { color: palette.fgMute }]} maxFontSizeMultiplier={1.3}>{meta}</Text> : null}
        {activity ? <Text style={[styles.meta, { color: palette.fgMute }]} numberOfLines={1} maxFontSizeMultiplier={1.3}>{activity}</Text> : null}
      </View>
    </View>
  );
  if (!children.length) {
    return agentLive ? null : <View style={styles.card}>{row("d", ICON.dispatched, "Background subagents dispatched")}</View>;
  }
  return (
    <View style={styles.card}>
      {children.map((c) => row(
        c.id, ICON[c.status], c.goal,
        [c.model, c.durationSeconds != null ? `${Math.round(c.durationSeconds)}s` : null].filter(Boolean).join(" · "),
        subagentActivity(c),
      ))}
    </View>
  );
}

/** The background subagents a run started outlive its card: they stay under the reply that replaced it. */
export function SavedDelegations({ state, entry }: { state: RoomSubagents; entry: MessageDelegations | undefined }) {
  return entry ? (
    <View style={styles.saved}>
      {entry.delegationIds.map((delegationId) => (
        <DelegateCard key={delegationId} state={state} agentId={entry.agentId} delegationId={delegationId} agentLive={false} />
      ))}
    </View>
  ) : null;
}

const styles = StyleSheet.create({
  saved: { marginLeft: AVATAR_GUTTER },
  card: { marginTop: spacing.sm, gap: spacing.xs },
  row: { flexDirection: "row", gap: spacing.sm },
  icon: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 18, width: 16, textAlign: "center" },
  text: { flex: 1 },
  goal: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 18 },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
});
