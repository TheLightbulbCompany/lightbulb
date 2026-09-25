import { sessionChanges, sessionPlan, type FileChange, type LocalSessionEvent, type PlanStatus } from "@lightbulb/shared";
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, fonts, spacing } from "../../config/theme";

const MARK: Record<PlanStatus, string> = { pending: "◻", in_progress: "◼", completed: "✔" };
const SAID: Record<PlanStatus, string> = { pending: "To do", in_progress: "In progress", completed: "Done" };

/**
 * The phone twin of the web SessionOverview: the live plan and "What changed",
 * pinned under the session summary. Both come from the loaded history.
 */
export function SessionOverview({ events }: { events: readonly LocalSessionEvent[] }) {
  const plan = useMemo(() => sessionPlan(events), [events]);
  const changes = useMemo(() => sessionChanges(events), [events]);
  const done = plan?.filter((step) => step.status === "completed").length ?? 0;
  const [planOpen, setPlanOpen] = useState<boolean | null>(null);
  const [changesOpen, setChangesOpen] = useState(false);
  if (!plan?.length && !changes.length) return null;
  // Open while work remains, until the reader decides.
  const showPlan = planOpen ?? done < (plan?.length ?? 0);
  const added = changes.reduce((n, f) => n + f.added, 0);
  const removed = changes.reduce((n, f) => n + f.removed, 0);
  return (
    <ScrollView style={styles.container} contentContainerStyle={styles.content}>
      {plan && plan.length > 0 && (
        <View>
          <Toggle label={`Plan · ${done} of ${plan.length} done`} open={showPlan} onPress={() => setPlanOpen(!showPlan)} />
          {showPlan && plan.map((step, i) => (
            <View key={i} style={styles.step} accessible accessibilityLabel={`${SAID[step.status]}: ${step.text}`}>
              <Text style={styles.mark}>{MARK[step.status]}</Text>
              <Text style={[styles.stepText, step.status === "in_progress" && styles.active, step.status === "completed" && styles.done]}>{step.text}</Text>
            </View>
          ))}
        </View>
      )}
      {changes.length > 0 && (
        <View>
          <Toggle
            label={`What changed · ${changes.length} ${changes.length === 1 ? "file" : "files"} +${added} −${removed}`}
            open={changesOpen}
            onPress={() => setChangesOpen(!changesOpen)}
          />
          {changesOpen && changes.map((file) => <FileRow key={file.path} file={file} />)}
        </View>
      )}
    </ScrollView>
  );
}

function Toggle({ label, open, onPress }: { label: string; open: boolean; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ expanded: open }} hitSlop={8}>
      <Text style={styles.toggle}>{`${label} ${open ? "⌄" : "›"}`}</Text>
    </Pressable>
  );
}

function FileRow({ file }: { file: FileChange }) {
  const [open, setOpen] = useState(false);
  const label = `${file.path} +${file.added} −${file.removed}`;
  return (
    <View style={styles.file}>
      <Pressable disabled={file.diff.length === 0} onPress={() => setOpen(!open)} accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ expanded: open }}>
        <Text style={styles.fileLine}>
          {file.path}<Text style={styles.add}>{` +${file.added}`}</Text><Text style={styles.del}>{` −${file.removed}`}</Text>
        </Text>
      </Pressable>
      {open && file.diff.map((line, i) => (
        <Text key={i} style={[styles.diff, line.startsWith("+") ? styles.add : line.startsWith("-") ? styles.del : null]}>{line}</Text>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { maxHeight: 240, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: colors.border },
  content: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm, gap: spacing.xs },
  toggle: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 20, color: colors.mute },
  step: { flexDirection: "row", gap: spacing.sm, paddingLeft: spacing.sm },
  mark: { fontFamily: fonts.mono, fontSize: 13, lineHeight: 19, color: colors.mute },
  stepText: { flex: 1, fontFamily: fonts.sans, fontSize: 13, lineHeight: 19, color: colors.ink },
  active: { fontFamily: fonts.sansSemiBold },
  done: { color: colors.mute, textDecorationLine: "line-through" },
  file: { paddingLeft: spacing.sm, paddingVertical: 2 },
  fileLine: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 18, color: colors.inkSoft },
  diff: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17, color: colors.mutedSecondary },
  add: { color: colors.green },
  del: { color: colors.error },
});
