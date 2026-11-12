// Needs you (the phone twin of web's components/voice/NeedsYou.tsx): one source, the viewer's open Questions
// (`GET /needs-you`), calls included — a call's "didn't know" and "wanted a person" are Questions too. The action is
// on the card; a "didn't know" takes a typed answer, which the server files into Knowledge.

import { ApiError, type QuestionView } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useCallback, useEffect, useState } from "react";
import { Linking, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { relativeTime } from "../../lib/relativeTime";
import { useLiveEvents } from "../../providers/live-events";

/** The viewer's open Questions, re-read on mount, every 30 s and on `reload()`. A workspace without the super-agent
 *  404s: no Questions, not an error. ponytail: two instances (tab badge, screen) poll separately; share one if it shows. */
export function useNeedsYou(): { items: QuestionView[] | null; failed: boolean; reload: () => void } {
  const { tasks } = useLiveEvents();
  const [items, setItems] = useState<QuestionView[] | null>(null);
  const [failed, setFailed] = useState(false);
  const reload = useCallback(() => {
    tasks.needsYou().then((r) => { setItems(r.items); setFailed(false); })
      .catch((e) => { if (e instanceof ApiError && e.status === 404) { setItems([]); setFailed(false); } else setFailed(true); });
  }, [tasks]);
  useEffect(() => {
    reload();
    const timer = setInterval(reload, 30_000);
    return () => clearInterval(timer);
  }, [reload]);
  return { items, failed, reload };
}

export function questionLabel(q: QuestionView): string {
  if (q.kind !== "question") return "Needs your OK";
  if (q.subkind === "handoff") return "Caller wanted a person";
  if (q.subkind === "line_problem") return "Line problem";
  if (q.subkind === "message") return "Message";
  return "Lightbulb didn't know";
}

function Button({ label, palette, primary, disabled, onPress }: {
  label: string; palette: ChatPalette; primary?: boolean; disabled?: boolean; onPress: () => void;
}) {
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ disabled: !!disabled }}
      disabled={disabled} onPress={onPress} hitSlop={6}
      style={({ pressed }) => [styles.button, {
        backgroundColor: primary ? palette.fg : "transparent", borderColor: primary ? palette.fg : palette.edge, opacity: pressed || disabled ? 0.6 : 1,
      }]}>
      <Text style={[styles.buttonLabel, { color: primary ? palette.onFg : palette.fg }]}>{label}</Text>
    </Pressable>
  );
}

export function NeedsYouCard({ question: q, palette, onDone }: { question: QuestionView; palette: ChatPalette; onDone: () => void }) {
  const { tasks } = useLiveEvents();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const caller = (q.preview as { caller?: string } | null | undefined)?.caller;
  const typed = q.options.some((o) => o.id === "answer");
  const send = async (optionId: string, answer?: string) => {
    setBusy(true); setErr(null);
    try {
      await tasks.answer(q.taskId, q.id, { optionId, clientAnswerId: randomUUID(), expectVersion: q.version, ...(answer ? { text: answer } : {}) });
      setDone(true); onDone();
    } catch (e) {
      const code = e instanceof ApiError ? (e.body as { detail?: { code?: string } } | null)?.detail?.code : undefined;
      setErr(code === "not_open" ? "Someone already answered this." : code === "not_answerer" ? "This one is waiting on someone else." : "Couldn't send. Try again.");
    } finally { setBusy(false); }
  };
  const option = (o: QuestionView["options"][number], primary: boolean) => o.id === "call_myself" && caller
    ? <Button key={o.id} label={o.label} palette={palette} onPress={() => void Linking.openURL(`tel:${caller}`)} />
    : <Button key={o.id} label={o.label} palette={palette} primary={primary} disabled={busy} onPress={() => void send(o.id)} />;
  const age = relativeTime(Date.parse(q.createdAt));
  return (
    <View accessibilityLabel={q.title} style={[styles.card, { borderColor: palette.edge, borderLeftColor: done ? palette.edge : palette.warn, backgroundColor: palette.surface }]}>
      <Text style={[styles.meta, { color: palette.fgMute }]}>{questionLabel(q)}{age ? ` · ${age}` : ""}</Text>
      <Text style={[styles.title, { color: palette.fg }]}>{q.title}</Text>
      {q.text && q.text !== q.title ? <Text style={[styles.meta, { color: palette.fgSoft }]}>{q.text}</Text> : null}
      {done ? <Text style={[styles.meta, { color: palette.fgMute }]}>Done.</Text>
        : !q.canAnswer ? <Text style={[styles.meta, { color: palette.fgMute }]}>{q.waitingLabel}</Text>
        : typed ? (
          <>
            <TextInput value={text} onChangeText={setText} multiline maxLength={4000} accessibilityLabel="Your answer"
              placeholder="Your answer" placeholderTextColor={palette.fgMute}
              style={[styles.input, { color: palette.fg, borderColor: palette.edge }]} />
            <View style={styles.actions}>
              <Button label="Answer" palette={palette} primary disabled={busy || !text.trim()} onPress={() => void send("answer", text.trim())} />
              {q.options.filter((o) => o.id !== "answer").map((o) => option(o, false))}
            </View>
          </>
        ) : <View style={styles.actions}>{q.options.map((o) => option(o, o.style === "primary"))}</View>}
      {err ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{err}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1, borderLeftWidth: 3, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm, gap: spacing.xs },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs, marginTop: spacing.xs },
  input: { borderWidth: 1, borderRadius: radii.sm, minHeight: 44, padding: spacing.sm, fontFamily: fonts.sans, fontSize: 15 },
  button: { minHeight: 44, minWidth: 44, paddingHorizontal: spacing.md, borderRadius: 999, borderWidth: 1, alignItems: "center", justifyContent: "center", alignSelf: "flex-start" },
  buttonLabel: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
});
