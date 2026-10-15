import { useAuth } from "@clerk/expo";
import { ideaEffortLine, orderIdeas, useIdeas, type Idea } from "@lightbulb/shared";
import { SymbolView } from "expo-symbols";
import { useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { BottomSheet } from "../ui/BottomSheet";

type IdeasSheetProps = {
  visible: boolean;
  onClose: () => void;
  /** Opened from an agent's DM: that agent's ideas come first. */
  agentId?: string;
  /** Start resolved: the DM where the agent posts its plan. */
  onStarted: (roomId: string) => void;
};

/**
 * Ideas on the phone (spec 2026-10-14 §4.4): the web pane's proposals as a list of cards, each
 * with one Start. Running ideas are left out; no prices (owner ruling and the App Store's).
 */
export function IdeasSheet({ visible, onClose, agentId, onStarted }: IdeasSheetProps) {
  const palette = useChatPalette();
  const { userId, orgId } = useAuth();
  const { messaging } = useLiveEvents();
  const { ideas, running, loading, error, start } = useIdeas(messaging, visible && userId ? `${userId}:${orgId ?? userId}` : null);
  const [starting, setStarting] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const runningIds = new Set(running.map((r) => r.ideaId));
  const shown = orderIdeas(ideas.filter((idea) => !runningIds.has(idea.id)), agentId);

  const onStart = async (idea: Idea) => {
    setStarting(idea.id);
    setStartError(null);
    try {
      const { roomId } = await start(idea.id);
      onStarted(roomId);
    } catch {
      setStartError("The idea could not start. Try again.");
    } finally {
      setStarting(null);
    }
  };

  return (
    <BottomSheet visible={visible} onClose={onClose} large>
      <View style={[styles.sheet, { backgroundColor: palette.surface }]}>
        <Text accessibilityRole="header" style={[styles.heading, { color: palette.fg }]}>Ideas</Text>
        {startError || error ? (
          <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>
            {startError ?? "Couldn't load ideas."}
          </Text>
        ) : null}
        {loading ? <ActivityIndicator /> : null}
        <ScrollView contentContainerStyle={styles.list}>
          {shown.map((idea) => (
            <View key={idea.id} accessibilityLabel={idea.title} style={[styles.card, { borderColor: palette.edge }]}>
              <Text style={[styles.meta, { color: palette.fgMute }]}>{idea.agentName} can do this for you</Text>
              <Text style={[styles.title, { color: palette.fg }]}>{idea.title}</Text>
              <Text style={[styles.body, { color: palette.fgSoft }]}>{idea.why}</Text>
              {idea.deliverables.map((line) => (
                <View key={line} style={styles.check}>
                  <SymbolView accessibilityElementsHidden name="checkmark" size={12} tintColor={palette.fgMute} />
                  <Text style={[styles.body, styles.checkText, { color: palette.fgSoft }]}>{line}</Text>
                </View>
              ))}
              <Text style={[styles.meta, { color: palette.fgSoft }]}>{ideaEffortLine(idea)}</Text>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={`Start: ${idea.title}`}
                disabled={starting !== null}
                onPress={() => void onStart(idea)}
                style={({ pressed }) => [styles.start, { backgroundColor: palette.fg }, (pressed || starting !== null) && styles.dim]}
              >
                <Text style={[styles.startText, { color: palette.onFg }]}>{starting === idea.id ? "Starting…" : "Start"}</Text>
              </Pressable>
            </View>
          ))}
          {!loading && !error && shown.length === 0 ? (
            <Text style={[styles.body, { color: palette.fgMute }]}>No ideas right now.</Text>
          ) : null}
        </ScrollView>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  sheet: { flex: 1, gap: spacing.md, padding: spacing.lg, paddingBottom: 0 },
  heading: { fontFamily: fonts.sansMedium, fontSize: 18, lineHeight: 24 },
  list: { gap: spacing.md, paddingBottom: spacing.xl },
  card: { borderWidth: 1, borderRadius: radii.md, padding: spacing.md, gap: spacing.xs },
  title: { fontFamily: fonts.sansMedium, fontSize: 17, lineHeight: 22 },
  body: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 20 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  check: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  checkText: { flex: 1 },
  start: { alignSelf: "flex-start", borderRadius: radii.sm, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm, marginTop: spacing.xs },
  startText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
  dim: { opacity: 0.6 },
});
