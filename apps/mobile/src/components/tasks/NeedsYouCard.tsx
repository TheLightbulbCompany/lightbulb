// The open question on a task (the phone twin of web's DecisionCard, the one "Needs you" card): who asks and when,
// the question title first, its context, then Deny / Allow once / Always allow from the same shared helpers. Only an
// answerer gets the buttons; everyone else reads who it waits on. Typed text never answers (contracts-v2 §8).

import { useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { askLine, decisionButtons, decisionMeta, type QuestionView, type TaskSummary } from "@lightbulb/shared";

import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { ActionButton } from "./TaskCard";
import { useTaskActions } from "./useTaskActions";

/** `onAnsweredElsewhere` gets the "Already answered by X" line: the refetch drops this card, so the screen shows it.
 *  `ended`: the request is Closed, so the card is read-only and says what became of it (web's N-F1 rule). */
export function NeedsYouCard({ task, question, palette, onAnsweredElsewhere, ended = false }: {
  task: TaskSummary; question: QuestionView; palette: ChatPalette; onAnsweredElsewhere?: (message: string) => void; ended?: boolean;
}) {
  const { answer } = useTaskActions();
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);
  const waiting = question.waitingLabel ?? `Waiting on ${question.answerers.map((a) => a.name).join(" or ")}`;
  const live = question.canAnswer && !closed && !ended && question.state === "open";
  const endedLabel = question.kind === "approval" ? "Closed — not approved" : question.kind === "review" ? "Closed — not sent" : "Closed — not answered";
  const heading = askLine(question, task.specialist);
  const buttons = decisionButtons(question.options, question.subkind, question.preview);
  const unavailable = buttons.find((b) => b.disabledReason)?.disabledReason;
  return (
    <View accessibilityLabel={heading} style={[styles.card, { borderColor: palette.edge, borderLeftColor: ended ? palette.edge : palette.warn, backgroundColor: palette.surface }]}>
      <Text style={[styles.meta, { color: palette.fgMute }]}>{ended ? endedLabel : question.canAnswer ? decisionMeta(question, task.specialist, undefined, Date.now()) : waiting}</Text>
      <Text style={[styles.title, { color: palette.fg }]}>{heading}</Text>
      {question.text && question.text !== heading ? <Text style={[styles.meta, { color: palette.fgMute }]}>{question.text}</Text> : null}
      {live ? (
        <View style={styles.actions}>
          {buttons.map((o) => (
            <ActionButton key={o.id} label={o.label} palette={palette} primary={o.style === "primary"} disabled={pending || !!o.disabledReason}
              onPress={async () => {
                setPending(true);
                const res = await answer(task, question.id, o.id, { expectVersion: question.version });
                setPending(false);
                if (res.ok) setClosed(true);
                else if (res.answeredBy) { setClosed(true); onAnsweredElsewhere?.(res.message); }
                else setNote(res.message);
              }} />
          ))}
        </View>
      ) : null}
      {live && unavailable ? <Text style={[styles.meta, { color: palette.fgMute }]}>{unavailable}</Text> : null}
      {note ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{note}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1, borderLeftWidth: 3, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm, gap: spacing.xs },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs, marginTop: spacing.xs },
});
