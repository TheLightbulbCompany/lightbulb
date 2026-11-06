import { useAuth } from "@clerk/expo";
import { specialistLabel, summaryView, useTask, useTaskQuestions, type TaskOutput } from "@lightbulb/shared";
import * as WebBrowser from "expo-web-browser";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
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
 * One task (mockup step 8, the phone): the specialist's name as the title, the task's title under it;
 * Stop / Resolve / Reopen; the Needs-you card
 * only while a question is open; the transcript (the task's hidden room) with a steer box; the outputs; Details in a sheet.
 */
export default function TaskScreen() {
  const { taskId } = useLocalSearchParams<{ taskId: string }>();
  const { userId } = useAuth();
  const palette = useChatPalette();
  const { tasks, agentNames, messaging, api } = useLiveEvents();
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
      <ConversationHeader title={task ? actor : "Task"} avatar={null} palette={palette}
        status={task?.title ?? null} statusTone="mute" />
    ),
  };

  if (!detail || !task) {
    return (
      <View style={[styles.container, { backgroundColor: palette.ground }]}>
        <Stack.Screen options={headerOptions} />
        <Text accessibilityRole="text" style={[styles.note, { color: palette.fgMute }]}>{error || !isValidating ? "This task isn\u2019t available to you." : "Loading…"}</Text>
      </View>
    );
  }
  const open = views?.items.find((q) => q.state === "open");
  // Stop only while something runs: a task that needs the viewer has nothing to stop.
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
        {/* One status word, the reason once: the title already names the specialist (audit 02 P2). */}
        <View style={styles.row} accessibilityLabel="Task status">
          <TaskStatus task={task} viewerId={userId ?? ""} palette={palette} />
          {view?.reason ? <Text style={[styles.meta, { color: palette.fgMute }]}>{view.reason}</Text> : null}
        </View>
        <View style={styles.row}>
          {live ? <ActionButton label="Stop" palette={palette} disabled={pending} onPress={() => void press("stop")} /> : null}
          {task.status !== "resolved"
            ? <ActionButton label="Close" palette={palette} disabled={pending} onPress={() => void press("resolve")} />
            : <ActionButton label="Reopen" palette={palette} disabled={pending} onPress={() => void press("reopen")} />}
          <ActionButton label="Details" palette={palette} onPress={() => setDetails(true)} />
        </View>
        {note ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{note}</Text> : null}
        {open ? <NeedsYouCard key={open.id} task={task} question={open} palette={palette} onAnsweredElsewhere={setNote} ended={view?.word === "Closed"} /> : null}
        {task.outputs?.length ? (
          <View style={[styles.output, { borderColor: palette.edge, backgroundColor: palette.surface }]} accessibilityLabel="Outputs">
            {task.outputs.map((o) => {
              const opens = canOpen(o);
              const row = <><Text style={[styles.meta, { color: palette.fg, flex: 1 }]} numberOfLines={1}>{o.title}</Text>
                <Text style={[styles.meta, { color: palette.fgMute }]}>{KIND_WORD[o.kind]}</Text></>;
              return opens ? (
                <Pressable key={o.id} accessibilityRole="button" accessibilityLabel={`Open ${o.title}`} style={styles.outputRow}
                  onPress={() => void openOutput(o, { attachmentUrl: messaging.attachmentUrl, appUrl: (id) => api.post(`/apps/${encodeURIComponent(id)}/open`, {}) as Promise<{ url: string }> })
                    .catch(() => setNote("That couldn\u2019t open. Try again."))}>{row}</Pressable>
              ) : <View key={o.id} style={styles.outputRow}>{row}</View>;
            })}
          </View>
        ) : null}
      </View>
      <TaskTranscript taskRoomId={detail.taskRoomId} focused={focused} />
      <TaskDetailsSheet detail={detail} visible={details} onClose={() => setDetails(false)} />
    </KeyboardAvoidingView>
  );
}

const KIND_WORD: Record<TaskOutput["kind"], string> = {
  app: "App", document: "Doc", file: "File", mail: "Mail", link: "Link", pr: "Link", capability: "Skill",
};

/** Mail and a capability have nothing to open beyond this task (its transcript shows them). */
const canOpen = (o: TaskOutput) => (o.kind === "app" && !!o.ref.appId) || ((o.kind === "document" || o.kind === "file") && !!o.ref.roomId && !!o.ref.attachmentId)
  || ((o.kind === "link" || o.kind === "pr") && /^https?:\/\//.test(o.ref.url ?? ""));

/** An output in the in-app browser: a link as is, an app through its open pass, a doc or file by its short-lived URL. */
async function openOutput(o: TaskOutput, deps: {
  attachmentUrl: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
  appUrl: (appId: string) => Promise<{ url: string }>;
}): Promise<void> {
  const url = o.kind === "app" ? (await deps.appUrl(o.ref.appId)).url
    : o.kind === "link" || o.kind === "pr" ? o.ref.url
      : (await deps.attachmentUrl(o.ref.roomId, o.ref.attachmentId)).url;
  await WebBrowser.openBrowserAsync(url);
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  top: { paddingHorizontal: spacing.lg, paddingTop: spacing.md, gap: spacing.sm },
  row: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  note: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, padding: spacing.lg },
  output: { borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.xs },
  outputRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm, minHeight: 44 },
});
