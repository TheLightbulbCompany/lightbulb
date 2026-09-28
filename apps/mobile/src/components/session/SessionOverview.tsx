import { sessionChanges, sessionPlan, type FileChange, type LocalSessionEvent, type PlanStatus } from "@lightbulb/shared";
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { fonts, spacing, useChatPalette, type ChatPalette } from "../../config/theme";

const MARK: Record<PlanStatus, string> = { pending: "◻", in_progress: "◼", completed: "✔" };
const SAID: Record<PlanStatus, string> = { pending: "To do", in_progress: "In progress", completed: "Done" };

/**
 * The phone twin of the web SessionOverview: the live plan and "What changed",
 * pinned under the session summary. Both come from the loaded history.
 *
 * `palette` defaults to the caller's own scheme via `useChatPalette` when
 * omitted, but the session sheet passes its own down so both stay in lockstep.
 */
export function SessionOverview({
  events,
  palette,
}: {
  events: readonly LocalSessionEvent[];
  palette?: ChatPalette;
}) {
  const schemePalette = useChatPalette();
  const p = palette ?? schemePalette;
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
    <ScrollView style={[styles.container, { borderBottomColor: p.edge }]} contentContainerStyle={styles.content}>
      {plan && plan.length > 0 && (
        <View>
          <Toggle label={`Plan · ${done} of ${plan.length} done`} open={showPlan} onPress={() => setPlanOpen(!showPlan)} palette={p} />
          {showPlan && plan.map((step, i) => (
            <View key={i} style={styles.step} accessible accessibilityLabel={`${SAID[step.status]}: ${step.text}`}>
              <Text style={[styles.mark, { color: p.fgMute }]}>{MARK[step.status]}</Text>
              <Text
                style={[
                  styles.stepText,
                  { color: p.fg },
                  step.status === "in_progress" && styles.active,
                  step.status === "completed" && [styles.done, { color: p.fgMute }],
                ]}
              >
                {step.text}
              </Text>
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
            palette={p}
          />
          {changesOpen && changes.map((file) => <FileRow key={file.path} file={file} palette={p} />)}
        </View>
      )}
    </ScrollView>
  );
}

function Toggle({ label, open, onPress, palette }: { label: string; open: boolean; onPress: () => void; palette: ChatPalette }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ expanded: open }} hitSlop={8}>
      <Text style={[styles.toggle, { color: palette.fgMute }]}>{`${label} ${open ? "⌄" : "›"}`}</Text>
    </Pressable>
  );
}

function FileRow({ file, palette }: { file: FileChange; palette: ChatPalette }) {
  const [open, setOpen] = useState(false);
  const label = `${file.path} +${file.added} −${file.removed}`;
  return (
    <View style={styles.file}>
      <Pressable disabled={file.diff.length === 0} onPress={() => setOpen(!open)} accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ expanded: open }}>
        <Text style={[styles.fileLine, { color: palette.fgSoft }]}>
          {file.path}
          <Text style={{ color: palette.live }}>{` +${file.added}`}</Text>
          <Text style={{ color: palette.alert }}>{` −${file.removed}`}</Text>
        </Text>
      </Pressable>
      {open && file.diff.map((line, i) => (
        <Text
          key={i}
          style={[
            styles.diff,
            { color: palette.fgSoft },
            line.startsWith("+") ? { color: palette.live } : line.startsWith("-") ? { color: palette.alert } : null,
          ]}
        >
          {line}
        </Text>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { maxHeight: 240, borderBottomWidth: StyleSheet.hairlineWidth },
  content: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm, gap: spacing.xs },
  toggle: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 20 },
  step: { flexDirection: "row", gap: spacing.sm, paddingLeft: spacing.sm },
  mark: { fontFamily: fonts.mono, fontSize: 13, lineHeight: 19 },
  stepText: { flex: 1, fontFamily: fonts.sans, fontSize: 13, lineHeight: 19 },
  active: { fontFamily: fonts.sansSemiBold },
  done: { textDecorationLine: "line-through" },
  file: { paddingLeft: spacing.sm, paddingVertical: 2 },
  fileLine: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 18 },
  diff: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17 },
});
