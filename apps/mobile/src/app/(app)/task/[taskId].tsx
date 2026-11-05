import { useAuth } from "@clerk/expo";
import { doneByLine, taskFinished, specialistLabel, summaryView, useTask, useTaskQuestions } from "@lightbulb/shared";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";

import { ConversationHeader } from "../../../components/conversation/ConversationHeader";
import { NeedsYouCard } from "../../../components/tasks/NeedsYouCard";
import { ActionButton, TaskStatus } from "../../../components/tasks/TaskCard";
import { TaskDetailsSheet } from "../../../components/tasks/TaskDetailsSheet";
import { TaskTranscript } from "../../../components/tasks/TaskTranscript";
import { useTaskActions } from "../../../components/tasks/useTaskActions";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { usePushedScreenKeyboardOffset } from "../../../lib/keyboardOffset";
import { useLiveEvents, useSuperAgent } from "../../../providers/live-events";

/**
 * One thread (mockup step 8, the phone): header with its specialist, word and where it runs;
 * Stop / Resolve / Reopen; the Needs-you card
 * only while a question is open; the transcript (the task's hidden room) with a steer box; the outputs; Details in a sheet.
 */
export default function TaskScreen() {
  const { taskId } = useLocalSearchParams<{ taskId: string }>();
  const { userId } = useAuth();
  const palette = useChatPalette();
  const { tasks, agentNames } = useLiveEvents();
  // Behind the switch: with it off (or still loading) a pasted task link fetches and marks nothing.
  const on = useSuperAgent();
  const id = on ? taskId ?? null : null;
  const { data: detail, error, isValidating } = useTask(tasks, id);
  const { data: views } = useTaskQuestions(tasks, detail?.questions.length ? id : null);
  const { act } = useTaskActions();
  const [focused, setFocused] = useState(true);
  const [details, setDetails] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  useFocusEffect(useCallback(() => { setFocused(true); return () => setFocused(false); }, []));
  // One read mark per open.
  useEffect(() => { if (id) void tasks.markRead(id).catch(() => undefined); }, [tasks, id]);
  const keyboardOffset = usePushedScreenKeyboardOffset();

  if (!on) return null;
  const task = detail?.task;
  const view = task ? summaryView(task, userId ?? "") : null;
  // The specialist is the actor ("Growth"): each is its own agent, so no "Lightbulb ·" in front.
  const actor = task ? specialistLabel(task.specialist, agentNames) : "";
  const headerOptions = {
    contentStyle: { backgroundColor: palette.ground },
    headerTitle: () => (
      <ConversationHeader title={task?.title ?? "Request"} avatar={null} palette={palette}
        status={task && view ? [actor, view.word, view.reason].filter(Boolean).join(" · ") : null}
        statusTone={view?.tone === "working" ? "live" : "mute"} />
    ),
  };

  if (!detail || !task) {
    return (
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
        <Stack.Screen options={headerOptions} />
        <Text accessibilityRole="text" style={[styles.note, { color: palette.fgMute }]}>{error || !isValidating ? "This request isn\u2019t available to you." : "Loading…"}</Text>
      </View>
    );
  }
  const open = views?.items.find((q) => q.state === "open");
  // The specialist as the actor: "Growth is working on this" / "Done by Growth" (founder, 5 Nov).
  // "Done by" only when the run really finished (web's rule, taskFinished); never on Closed.
  const doneBy = view ? doneByLine(task.specialist, view, agentNames, taskFinished(detail)) : null;
  // Stop only while something runs: a request that needs the viewer has nothing to stop.
  const live = view?.word === "Working";
  const press = async (verb: "stop" | "resolve" | "reopen") => {
    setPending(true);
    const res = await act(task, verb);
    setPending(false);
    setNote(res.ok ? null : res.message);
  };
  return (
    <KeyboardAvoidingView style={[styles.container, { backgroundColor: palette.ground }]} behavior="padding" keyboardVerticalOffset={keyboardOffset}>
      <Stack.Screen options={headerOptions} />
      <View style={styles.top}>
        <View style={styles.row} accessibilityLabel="Request status">
          <TaskStatus task={task} viewerId={userId ?? ""} palette={palette} />
        </View>
        <View style={styles.row}>
          {live ? <ActionButton label="Stop" palette={palette} disabled={pending} onPress={() => void press("stop")} /> : null}
          {task.status !== "resolved"
            ? <ActionButton label="Close" palette={palette} disabled={pending} onPress={() => void press("resolve")} />
            : <ActionButton label="Reopen" palette={palette} disabled={pending} onPress={() => void press("reopen")} />}
          <ActionButton label="Details" palette={palette} onPress={() => setDetails(true)} />
        </View>
        {doneBy ? <Text style={[styles.meta, { color: palette.fgMute }]}>{doneBy}</Text> : null}
        {note ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{note}</Text> : null}
        {open ? <NeedsYouCard key={open.id} task={task} question={open} palette={palette} onAnsweredElsewhere={setNote} ended={view?.word === "Closed"} /> : null}
        {task.output ? (
          <View style={[styles.output, { borderColor: palette.edge, backgroundColor: palette.surface }]} accessibilityLabel="Outputs">
            <Text style={[styles.meta, { color: palette.fg }]} numberOfLines={1}>📎 {task.output.title}</Text>
          </View>
        ) : null}
      </View>
      <TaskTranscript taskRoomId={detail.taskRoomId} focused={focused} />
      <TaskDetailsSheet detail={detail} visible={details} onClose={() => setDetails(false)} />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  top: { paddingHorizontal: spacing.lg, paddingTop: spacing.md, gap: spacing.sm },
  row: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  note: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, padding: spacing.lg },
  output: { borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
});
