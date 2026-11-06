// The card under Lightbulb's "Set up below. Turn it on with the button on the card." (the web twin:
// components/tasks/AutomationConfirmCard): the schedule in the viewer's zone (never raw cron), the next run, who runs it
// and what it may spend. Turn on shows whenever it is off and the viewer may turn it on; typed text never does
// (contracts-v2 §8). Not now deletes the draft.

import { useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import {
  automationStatusLine, budgetLine, canDecide, canEdit, nextRunWords, scheduleWords, specialistLabel, useAutomation, viewerTimeZone, waitingWords,
} from "@lightbulb/shared";

import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { useAutomationActions, useAutomationSource, useAutomationViewer } from "./automations";
import { ActionButton } from "./TaskCard";
import { errorCopy } from "./useTaskActions";

export function AutomationConfirmCard({ automationId, palette, viewerTz = viewerTimeZone() }: {
  automationId: string; palette: ChatPalette; viewerTz?: string;
}) {
  const { agentNames } = useLiveEvents();
  const { data: a, error, mutate } = useAutomation(useAutomationSource(), automationId);
  const { act, remove } = useAutomationActions();
  const viewer = useAutomationViewer();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  // Not now deletes the draft, so a later read is a 404 (dev walk 10, W10-6): a plain line, never a dead box.
  if (gone || (!a && (error as { status?: number } | undefined)?.status === 404)) {
    return <Text style={[styles.meta, { color: palette.fgMute }]}>This schedule was removed.</Text>;
  }
  if (!a && error) {
    return (
      <Text style={[styles.meta, { color: palette.fgMute }]}>
        This automation couldn’t load.{" "}
        <Text accessibilityRole="button" onPress={() => void mutate()} style={{ textDecorationLine: "underline" }}>Retry</Text>
      </Text>
    );
  }
  // The card's own height while it loads, so the conversation doesn't jump when it fills in.
  if (!a) return <View accessibilityLabel="Loading automation" accessibilityState={{ busy: true }} style={[styles.card, styles.skeleton, { borderColor: palette.edge, backgroundColor: palette.chip }]} />;
  const decide = canDecide(a, viewer);
  const edit = canEdit(a, viewer);
  const run = async (what: "on" | "off") => {
    setPending(true);
    setNote(null);
    try {
      if (what === "on") await act(a.id, "activate");
      else { await remove(a.id); setGone(true); }
    } catch (e) {
      setNote(errorCopy(e));
    } finally {
      setPending(false);
    }
  };
  // An unreadable date reads as nothing, never a bare "Next:".
  const next = a.nextRuns[0] ? nextRunWords(a.nextRuns[0], viewerTz) : "";
  const meta = [scheduleWords(a, viewerTz), specialistLabel(a.specialistId, agentNames), budgetLine(a)].filter(Boolean).join(" · ");
  return (
    <View accessibilityLabel={`Turn on ${a.name}`} style={[styles.card, { borderColor: palette.edge, backgroundColor: palette.surface }]}>
      <Text style={[styles.title, { color: palette.fg }]}>{a.status === "draft" ? "Turn on: " : ""}{a.name}</Text>
      <Text style={[styles.meta, { color: palette.fgMute }]}>{meta}</Text>
      {next ? <Text style={[styles.meta, { color: palette.fgMute }]}>Next: {next}</Text> : null}
      {a.status !== "draft" ? (
        // Only On is green; Paused is a state, not a success.
        <Text style={[styles.meta, { color: a.status === "active" ? palette.live : palette.fgMute }]}>{automationStatusLine(a) ?? "On"}</Text>
      ) : null}
      {a.status !== "active" ? (
        // The server's rules (routers/automations.py): Turn on is an activate, also from paused; Not now is an edit
        // (admin or owner) and only drops a draft.
        <View style={styles.actions}>
          {decide ? <ActionButton label="Turn on" palette={palette} primary disabled={pending} onPress={() => void run("on")} />
            : <Text style={[styles.meta, { color: palette.fgMute }]}>{waitingWords(a)}</Text>}
          {edit && a.status === "draft" ? <ActionButton label="Not now" palette={palette} disabled={pending} onPress={() => void run("off")} /> : null}
        </View>
      ) : null}
      {note ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{note}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.sm, paddingVertical: spacing.xs, gap: spacing.xs, maxWidth: 520 },
  skeleton: { height: 96 },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  actions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.xs },
});
