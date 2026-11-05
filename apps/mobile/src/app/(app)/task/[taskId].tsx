import { useAuth } from "@clerk/expo";
import { specialistLabel, summaryView, useTask, useTaskQuestions } from "@lightbulb/shared";
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
import { useLiveEvents } from "../../../providers/live-events";

/**
 * One thread (mockup step 8, the phone): header with its specialist, word and where it runs;
 * Stop / Resolve / Reopen; the Needs-you card only while a question is open; the transcript
 * (the task's hidden room) with a steer box; the outputs; Details in a sheet.
 */
export default function TaskScreen() {
  const { taskId } = useLocalSearchParams<{ taskId: string }>();
  const { userId } = useAuth();
  const palette = useChatPalette();
  const { tasks } = useLiveEvents();
  const { data: detail, error } = useTask(tasks, taskId ?? null);
  const { data: views } = useTaskQuestions(tasks, detail?.questions.length ? taskId ?? null : null);
  const { act } = useTaskActions();
  const [focused, setFocused] = useState(true);
  const [details, setDetails] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  useFocusEffect(useCallback(() => { setFocused(true); return () => setFocused(false); }, []));
  // One read mark per open.
  useEffect(() => { if (taskId) void tasks.markRead(taskId).catch(() => undefined); }, [tasks, taskId]);
  const keyboardOffset = usePushedScreenKeyboardOffset();

  const task = detail?.task;
  const view = task ? summaryView(task, userId ?? "") : null;
  const headerOptions = {
    contentStyle: { backgroundColor: palette.ground },
    headerTitle: () => (
      <ConversationHeader title={task?.title ?? "Thread"} avatar={null} palette={palette}
        status={task ? `${specialistLabel(task.specialist)} · ${view?.word} · Lightbulb cloud` : null}
        statusTone={view?.tone === "working" ? "live" : "mute"} />
    ),
  };

  if (!detail || !task) {
    return (
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
        <Stack.Screen options={headerOptions} />
        <Text accessibilityRole="text" style={[styles.note, { color: palette.fgMute }]}>{error ? "This thread isn\u2019t available to you." : "Loading…"}</Text>
      </View>
    );
  }
  const open = views?.items.find((q) => q.state === "open");
  const live = task.status === "queued" || task.status === "working" || task.status === "waiting";
  const press = async (verb: "stop" | "resolve" | "reopen") => {
    const res = await act(task, verb);
    setNote(res.ok ? null : res.message);
  };
  return (
    <KeyboardAvoidingView style={[styles.container, { backgroundColor: palette.ground }]} behavior="padding" keyboardVerticalOffset={keyboardOffset}>
      <Stack.Screen options={headerOptions} />
      <View style={styles.top}>
        <View style={styles.row} accessibilityLabel="Thread status">
          <TaskStatus task={task} viewerId={userId ?? ""} palette={palette} />
          <Text style={[styles.meta, { color: palette.fgMute }]}>☁ Lightbulb cloud</Text>
        </View>
        <View style={styles.row}>
          {live ? <ActionButton label="Stop" palette={palette} onPress={() => void press("stop")} /> : null}
          {task.status !== "resolved"
            ? <ActionButton label="Resolve" palette={palette} onPress={() => void press("resolve")} />
            : <ActionButton label="Reopen" palette={palette} onPress={() => void press("reopen")} />}
          <ActionButton label="Details" palette={palette} onPress={() => setDetails(true)} />
        </View>
        {note ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{note}</Text> : null}
        {open ? <NeedsYouCard task={task} question={open} palette={palette} /> : null}
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
  note: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, padding: spacing.lg },
  output: { borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
});
