// Folded details of a thread (web's TaskDetails): why it started, attempts, cost, effect results,
// and the trace, read only when asked for.

import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { effectLine, type TaskDetail, type TaskTrace } from "@lightbulb/shared";

import { fonts, spacing, useChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { BottomSheet } from "../ui/BottomSheet";

const money = (n: number) => `$${n.toFixed(2).replace(/\.00$/, "")}`;

export function TaskDetailsSheet({ detail, visible, onClose }: { detail: TaskDetail; visible: boolean; onClose: () => void }) {
  const palette = useChatPalette();
  const { tasks } = useLiveEvents();
  const [trace, setTrace] = useState<TaskTrace | { error: true } | null | undefined>(undefined);
  const { task, attempts, effects } = detail;
  const latest = attempts.reduce((n, a) => Math.max(n, a.n), 0);
  const cost = task.budgetUsd != null || task.costUsd
    ? `${money(task.costUsd ?? 0)}${task.budgetUsd != null ? ` of ${money(task.budgetUsd)} budget` : ""}` : null;
  const loadTrace = () => {
    if (trace !== undefined) return;
    setTrace(null);
    void tasks.trace(task.taskId, latest).then(setTrace, () => setTrace({ error: true }));
  };
  return (
    <BottomSheet visible={visible} onClose={onClose}>
      <ScrollView contentContainerStyle={styles.body} accessibilityLabel="Details">
        <Text style={[styles.heading, { color: palette.fg }]}>Details</Text>
        <Row label="Attempts" value={String(attempts.length)} />
        {cost ? <Row label="Cost" value={cost} /> : null}
        {effects.length > 0 ? (
          <View style={styles.block} accessibilityLabel="Effects">
            <Text style={[styles.label, { color: palette.fgMute }]}>EFFECTS</Text>
            {effects.map((e) => {
              const line = effectLine(e);
              const warn = e.state === "unknown" || line.startsWith("Didn't happen");
              return <Text key={e.idemKey} style={[styles.value, { color: palette.fg }]}>{e.output?.title ?? e.tool} · <Text style={{ color: warn ? palette.warn : palette.fgMute }}>{line}</Text></Text>;
            })}
          </View>
        ) : null}
        {latest > 0 ? (
          <View style={styles.block}>
            <Pressable accessibilityRole="button" accessibilityLabel="Trace" onPress={loadTrace} hitSlop={8}>
              <Text style={[styles.label, { color: palette.link }]}>TRACE</Text>
            </Pressable>
            {trace === null ? <Text style={[styles.value, { color: palette.fgMute }]}>Loading…</Text>
              : trace && "error" in trace ? <Text style={[styles.value, { color: palette.fgMute }]}>{"The trace isn\u2019t available."}</Text>
              : trace && trace.recentTools.length === 0 ? <Text style={[styles.value, { color: palette.fgMute }]}>No tool calls yet.</Text>
              : trace ? trace.recentTools.map((tool, i) => <Text key={i} style={[styles.mono, { color: palette.fg }]} numberOfLines={1}>{toolLine(tool)}</Text>) : null}
          </View>
        ) : null}
      </ScrollView>
    </BottomSheet>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  const palette = useChatPalette();
  return (
    <View style={styles.block}>
      <Text style={[styles.label, { color: palette.fgMute }]}>{label.toUpperCase()}</Text>
      <Text style={[styles.value, { color: palette.fg }]}>{value}</Text>
    </View>
  );
}

function toolLine(tool: unknown): string {
  if (typeof tool === "string") return tool;
  if (tool && typeof tool === "object") {
    const t = tool as Record<string, unknown>;
    const name = [t.name, t.tool, t.type].find((v) => typeof v === "string") as string | undefined;
    if (name) return `${name}${typeof t.status === "string" ? ` · ${t.status}` : ""}`;
  }
  return JSON.stringify(tool);
}

const styles = StyleSheet.create({
  body: { padding: spacing.lg, gap: spacing.md },
  heading: { fontFamily: fonts.sansSemiBold, fontSize: 17, lineHeight: 22 },
  block: { gap: 2 },
  label: { fontFamily: fonts.sansSemiBold, fontSize: 10, letterSpacing: 1.2, lineHeight: 14 },
  value: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  mono: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 16 },
});
