import {
  automationStatusLine, budgetLine, canDecide, canEdit, runNowReason, scheduleWords, specialistLabel, summaryView, viewerTimeZone, waitingWords,
} from "@lightbulb/shared";
import { Stack, useLocalSearchParams } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { ActionButton, openTaskScreen } from "../../../components/tasks/TaskCard";
import { errorCopy } from "../../../components/tasks/useTaskActions";
import { runNow, useAutomation, useAutomationActions, useAutomationRuns, useAutomationViewer } from "../../../components/tasks/automations";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { useSuperAgent } from "../../../providers/live-events";

/**
 * One automation (mockup step 11 on the phone): its schedule in the viewer's zone, instructions,
 * who runs it and what it may spend, Pause/Resume and Run now under the server's two rules, and a
 * history in which every run is its own thread.
 */
export default function AutomationScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const palette = useChatPalette();
  const viewer = useAutomationViewer();
  // Behind the switch: off (or loading), a pasted link fetches nothing.
  const on = useSuperAgent();
  const { data: a, error, isValidating } = useAutomation(on ? id ?? null : null);
  const { data: runs } = useAutomationRuns(on ? id ?? null : null);
  const { act } = useAutomationActions();
  const [note, setNote] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const viewerTz = viewerTimeZone();

  if (!on) return null;

  if (!a) {
    return (
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
        <Stack.Screen options={{ title: "Automation" }} />
        <Text style={[styles.meta, { color: palette.fgMute, padding: spacing.lg }]}>{error || !isValidating ? "This automation isn’t available." : "Loading…"}</Text>
      </View>
    );
  }
  const paused = a.status === "paused" || a.status === "paused_system";
  const edit = canEdit(a, viewer);
  const decide = canDecide(a, viewer);
  const press = async (verb: "pause" | "resume") => {
    setPending(true);
    try { await act(a.id, verb); setNote(null); } catch (e) { setNote(errorCopy(e)); } finally { setPending(false); }
  };
  const runReason = runNowReason(a, edit);
  return (
    <ScrollView style={[styles.container, { backgroundColor: palette.ground }]} contentContainerStyle={styles.body} contentInsetAdjustmentBehavior="automatic">
      <Stack.Screen options={{ title: a.name }} />
      <Text style={[styles.meta, { color: palette.fgMute }]}>
        {[scheduleWords(a, viewerTz) ?? "No schedule", specialistLabel(a.specialistId), budgetLine(a)].filter(Boolean).join(" · ")}
      </Text>
      {automationStatusLine(a) ? <Text style={[styles.meta, { color: palette.warn }]}>{automationStatusLine(a)}</Text> : null}
      <View style={styles.actions}>
        {a.status !== "draft" && !paused && edit ? <ActionButton label="Pause" palette={palette} disabled={pending} onPress={() => void press("pause")} /> : null}
        {paused && decide ? <ActionButton label="Resume" palette={palette} disabled={pending} onPress={() => void press("resume")} /> : null}
        {paused && !decide ? <Text style={[styles.meta, { color: palette.fgMute }]}>{waitingWords(a)}</Text> : null}
        {!a.match ? (a.activeRunTaskId
          ? <ActionButton label="Running ›" palette={palette} onPress={() => openTaskScreen(a.activeRunTaskId!)} />
          : <ActionButton label="Run now" palette={palette} primary disabled={!!runReason || pending} onPress={async () => {
            setPending(true);
            const res = await runNow(act, a);
            setPending(false);
            if (res.kind === "running") openTaskScreen(res.taskId);
            setNote(res.kind === "started" ? "Running now." : res.kind === "note" ? res.message : null);
          }} />) : null}
        {runReason && !a.match ? <Text style={[styles.meta, { color: palette.fgMute }]}>{runReason}</Text> : null}
      </View>
      {note ? <Text accessibilityRole="text" style={[styles.meta, { color: palette.fgMute }]}>{note}</Text> : null}
      <View style={[styles.card, { borderColor: palette.edge, backgroundColor: palette.surface }]} accessibilityLabel="Instructions">
        <Text style={[styles.label, { color: palette.fgMute }]}>INSTRUCTIONS</Text>
        <Text style={[styles.body2, { color: palette.fg }]}>{a.instructions}</Text>
      </View>
      <View style={styles.section} accessibilityLabel="History">
        <Text style={[styles.label, { color: palette.fgMute }]}>HISTORY</Text>
        {!runs?.runs.length ? <Text style={[styles.meta, { color: palette.fgMute }]}>No runs yet. Each run opens as its own thread.</Text>
          : <View style={[styles.list, { borderColor: palette.edge, backgroundColor: palette.surface }]}>{runs.runs.map((t, i) => (
            <Pressable key={t.taskId} accessibilityRole="button" accessibilityLabel={`Open ${t.title}`} onPress={() => openTaskScreen(t.taskId)}
              style={({ pressed }) => [styles.row, { backgroundColor: pressed ? palette.hover : "transparent", borderColor: palette.edge, borderTopWidth: i === 0 ? 0 : StyleSheet.hairlineWidth }]}>
              <Text style={[styles.title, { color: palette.fg }]} numberOfLines={1}>{t.title}</Text>
              <Text style={[styles.meta, { color: palette.fgMute }]}>{summaryView(t, viewer.userId ?? "").word}</Text>
            </Pressable>
          ))}</View>}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  body: { padding: spacing.lg, gap: spacing.md },
  actions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm },
  card: { borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm, gap: spacing.xs },
  section: { gap: spacing.xs },
  list: { borderWidth: 1, borderRadius: radii.md, overflow: "hidden" },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.sm, paddingHorizontal: spacing.md, paddingVertical: spacing.sm, minHeight: 44 },
  title: { flex: 1, fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  label: { fontFamily: fonts.sansSemiBold, fontSize: 10, letterSpacing: 1.2, lineHeight: 14 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  body2: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
});
