import { useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, useColorScheme, View } from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import type { ExecutionBlocker } from "../../lib/teamsData";

// Resume a stopped task (web ExecutionBlockerNotice parity). After a Stop,
// Paperclip refuses a generic Retry and continues only once a person says what
// the stopped run got done; it then starts a new run on the same task.

/** The person's answer -> upstream's actionOutcome. */
const OUTCOMES = [
  { value: "not_performed", label: "Nothing done" },
  { value: "mixed", label: "Partly" },
  { value: "completed", label: "Done" },
] as const;
type ActionOutcome = (typeof OUTCOMES)[number]["value"];

export function ResumeStoppedRunCard({
  blocker,
  onResume,
}: {
  blocker: ExecutionBlocker;
  /** Posts the resolve body; resolves to an error message, or null on success. */
  onResume: (body: Record<string, unknown>) => Promise<string | null>;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const [outcome, setOutcome] = useState<ActionOutcome | null>(null);
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resume = async () => {
    const choice = OUTCOMES.find((o) => o.value === outcome);
    if (!choice || pending) return;
    setPending(true);
    setError(null);
    // Upstream wants >= 20 chars of evidence; the choice itself always gives that.
    const answer = `A person answered: ${choice.label.toLowerCase()}.`;
    const trimmed = note.trim();
    const failure = await onResume({
      actionId: blocker.recoveryActionId,
      outcome: "restored",
      sourceIssueStatus: "todo",
      executionReconciliation: {
        runId: blocker.runId,
        providerStopped: true,
        actionOutcome: choice.value,
        outcomeEvidence: trimmed ? `${answer} ${trimmed}` : answer,
      },
    });
    setError(failure);
    setPending(false);
  };

  return (
    <View style={styles.card} accessibilityLabel="Task recovery">
      <Text style={styles.title}>This run was stopped. Did it finish what it was doing?</Text>
      <View style={styles.choices} accessibilityRole="radiogroup">
        {OUTCOMES.map((o) => {
          const selected = outcome === o.value;
          return (
            <Pressable
              key={o.value}
              onPress={() => setOutcome(o.value)}
              disabled={pending}
              accessibilityRole="radio"
              accessibilityState={{ checked: selected, disabled: pending }}
              accessibilityLabel={o.label}
              style={({ pressed }) => [styles.choice, selected && styles.choiceOn, pressed && styles.pressed]}
            >
              <Text style={[styles.choiceText, selected && styles.choiceTextOn]}>{o.label}</Text>
            </Pressable>
          );
        })}
      </View>
      <TextInput
        style={styles.note}
        value={note}
        onChangeText={setNote}
        editable={!pending}
        multiline
        maxLength={11000}
        placeholder="Note for the agent (optional)"
        placeholderTextColor={palette.fgMute}
        accessibilityLabel="Note for the agent (optional)"
      />
      <Pressable
        onPress={() => void resume()}
        disabled={!outcome || pending}
        accessibilityRole="button"
        accessibilityLabel="Resume"
        accessibilityState={{ disabled: !outcome || pending }}
        style={({ pressed }) => [styles.resume, (!outcome || pending) && styles.disabled, pressed && styles.pressed]}
      >
        <Text style={styles.resumeText}>{pending ? "Resuming…" : "Resume"}</Text>
      </Pressable>
      {error ? <Text style={styles.error}>{error}</Text> : null}
    </View>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    card: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.md,
      gap: spacing.md,
    },
    title: { fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 19, color: p.fg },
    choices: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm },
    choice: {
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.sm,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: p.edge,
      backgroundColor: p.surface,
    },
    choiceOn: { backgroundColor: p.fg, borderColor: p.fg },
    choiceText: { fontFamily: fonts.sansMedium, fontSize: 12, lineHeight: 16, color: p.fg },
    choiceTextOn: { color: p.onFg },
    note: {
      minHeight: 60,
      borderRadius: radii.sm,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.sm,
      fontFamily: fonts.sans,
      fontSize: 13,
      color: p.fg,
      textAlignVertical: "top",
    },
    resume: {
      alignSelf: "flex-start",
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.sm,
      borderRadius: radii.sm,
      backgroundColor: p.fg,
    },
    resumeText: { fontFamily: fonts.sansMedium, fontSize: 12, lineHeight: 16, color: p.onFg },
    disabled: { opacity: 0.5 },
    pressed: { opacity: 0.6 },
    error: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: p.alert },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
