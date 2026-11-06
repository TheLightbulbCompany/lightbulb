// Thread cards under a Lightbulb message (the phone twin of web's components/tasks/TaskCard.tsx):
// the whole card opens the thread, the one action sits inside it, state colour marks the word and
// a dot, never a fill. 44pt targets.

import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { router } from "expo-router";
import {
  specialistLabel, summaryView, taskSummaryLine, useTask, useTaskList, type CanonicalRoomMessage, type TaskSummary, type TaskTone,
} from "@lightbulb/shared";

import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { ACTION_LABELS, useTaskActions } from "./useTaskActions";

export const openTaskScreen = (taskId: string) =>
  router.push({ pathname: "/(app)/task/[taskId]", params: { taskId } });

const toneColor = (tone: TaskTone, palette: ChatPalette): string =>
  tone === "working" ? palette.link : tone === "waiting" ? palette.warn : tone === "landing" ? palette.live : tone === "review" ? palette.fg : palette.fgMute;

export function TaskStatus({ task, viewerId, palette }: { task: TaskSummary; viewerId: string; palette: ChatPalette }) {
  const view = summaryView(task, viewerId);
  const color = toneColor(view.tone, palette);
  return (
    <View style={styles.status} accessibilityLabel={view.word}>
      <View style={[styles.dot, { backgroundColor: color }]} />
      <Text style={[styles.statusWord, { color }]}>{view.word}</Text>
    </View>
  );
}

export function TaskCard({ task, taskId, viewerId, palette, onOpen = openTaskScreen }: {
  task: TaskSummary | undefined; taskId: string; viewerId: string; palette: ChatPalette;
  onOpen?: (taskId: string) => void;
}) {
  const { run } = useTaskActions();
  const { agentNames } = useLiveEvents();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!task) {
    return (
      <View style={[styles.card, styles.unavailable, { borderColor: palette.edge }]} testID={`task-card-${taskId}`}>
        <Text style={[styles.meta, { color: palette.fgMute }]}>{"This task isn't available"}</Text>
      </View>
    );
  }
  const view = summaryView(task, viewerId);
  // Reopen lives on the request screen and the Work list, not on every resolved card in the history (audit #30).
  const inline = view.action && view.action !== "answer" && view.action !== "review" && view.action !== "reopen" ? view.action : null;
  // The specialist is the actor, by its own name ("Growth"), on its own line (founder, 5 Nov).
  const line = taskSummaryLine(task, viewerId);
  const detail = line === view.word ? null : line;
  // The open target and the action are siblings, never nested: a screen reader reaches each on its own (audit #23).
  return (
    <View style={[styles.card, { borderColor: palette.edge, backgroundColor: palette.surface }]} testID={`task-card-${taskId}`}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Open ${task.title}, ${view.word}`}
        onPress={() => onOpen(taskId)}
        style={({ pressed }) => [styles.open, pressed ? { backgroundColor: palette.hover } : null]}
      >
      <View style={styles.row}>
        <View style={styles.main}>
          <Text style={[styles.title, { color: palette.fg }]} numberOfLines={1}>{task.title}</Text>
          <Text style={[styles.meta, { color: palette.fgMute }]} numberOfLines={1}>{specialistLabel(task.specialist, agentNames)}</Text>
          {detail ? <Text style={[styles.meta, { color: palette.fg }]} numberOfLines={2}>{detail}</Text> : null}
          {task.output && !detail?.includes(task.output.title) ? (
            <Text style={[styles.meta, { color: palette.fgMute }]} numberOfLines={1}>📎 {task.output.title}</Text>
          ) : null}
        </View>
        <TaskStatus task={task} viewerId={viewerId} palette={palette} />
      </View>
      </Pressable>
      {inline ? (
        <ActionButton label={ACTION_LABELS[inline]} palette={palette} primary={view.tone === "waiting" || view.tone === "review"} disabled={pending}
          onPress={async () => {
            // An answer that acts outside (approve, send, publish, merge) is decided on the thread screen, where the
            // question is shown in full: the card never sends it blind.
            if (inline !== "retry") { onOpen(taskId); return; }
            setPending(true);
            setError(null);
            const res = await run(task, inline);
            setPending(false);
            if (!res.ok) setError(res.message);
          }} />
      ) : null}
      {error ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{error}</Text> : null}
    </View>
  );
}

export function ActionButton({ label, palette, primary, disabled, onPress }: {
  label: string; palette: ChatPalette; primary?: boolean; disabled?: boolean; onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: !!disabled }}
      disabled={disabled}
      onPress={(event) => { event.stopPropagation?.(); onPress(); }}
      hitSlop={6}
      style={({ pressed }) => [styles.button, {
        backgroundColor: primary ? palette.fg : "transparent", borderColor: primary ? palette.fg : palette.edge, opacity: pressed || disabled ? 0.6 : 1,
      }]}
    >
      <Text style={[styles.buttonLabel, { color: primary ? palette.onFg : palette.fg }]}>{label}</Text>
    </Pressable>
  );
}

/** Thread cards under a Lightbulb message (`taskRefs`). One shared room read. */
export function MessageTasks({ message, viewerId, palette, onOpen }: {
  message: CanonicalRoomMessage; viewerId: string; palette: ChatPalette; onOpen?: (taskId: string) => void;
}) {
  const { tasks } = useLiveEvents();
  const refs = message.taskRefs ?? [];
  const { data } = useTaskList(tasks, refs.length ? { kind: "room", roomId: message.roomId } : null);
  if (!refs.length || !data) return null;
  const byId = new Map(data.tasks.map((t) => [t.taskId, t]));
  return (
    <View style={styles.list} accessibilityLabel="Tasks in this message">
      {refs.map((r) => <RefCard key={r.taskId} taskId={r.taskId} listed={byId.get(r.taskId)} viewerId={viewerId} palette={palette} onOpen={onOpen} />)}
    </View>
  );
}

/** A card whose task the room's first page doesn't hold (an older thread) reads it by id. */
function RefCard({ taskId, listed, viewerId, palette, onOpen }: {
  taskId: string; listed: TaskSummary | undefined; viewerId: string; palette: ChatPalette; onOpen?: (taskId: string) => void;
}) {
  const { tasks } = useLiveEvents();
  const { data, error } = useTask(tasks, listed ? null : taskId);
  if (!listed && !data && !error) return null;
  return <TaskCard task={listed ?? data?.task} taskId={taskId} viewerId={viewerId} palette={palette} onOpen={onOpen} />;
}

/** "Sent to Workshop signup app ›" under a person's message (one request opens it; several name the count). */
export function RoutedToLine({ message, palette, onOpen = openTaskScreen }: {
  message: CanonicalRoomMessage; palette: ChatPalette; onOpen?: (taskId: string) => void;
}) {
  const { tasks } = useLiveEvents();
  const routedTo = message.routedTo;
  const single = !!routedTo && (routedTo.passThrough || routedTo.taskIds.length === 1);
  const { data } = useTaskList(tasks, single ? { kind: "room", roomId: message.roomId } : null);
  if (!routedTo || !routedTo.taskIds.length) return null;
  const { taskIds, started } = routedTo;
  const label = single
    ? `Sent to ${data?.tasks.find((t) => t.taskId === taskIds[0])?.title ?? "a task"}`
    : `${started} ${started === 1 ? "task" : "tasks"} started`;
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`${label} ›`} hitSlop={8} disabled={!single} onPress={() => onOpen(taskIds[0])}>
      <Text style={[styles.meta, { color: palette.fgMute }]}>{label} ›</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  list: { gap: spacing.xs, marginTop: spacing.xs },
  card: { borderWidth: 1, borderRadius: radii.md, paddingHorizontal: spacing.sm, paddingVertical: spacing.xs, gap: spacing.xs, maxWidth: 520 },
  unavailable: { borderStyle: "dashed" },
  open: { borderRadius: radii.sm, marginHorizontal: -spacing.xs, paddingHorizontal: spacing.xs, minHeight: 44, justifyContent: "center" },
  row: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  main: { flex: 1, minWidth: 0, gap: 2 },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  label: { fontFamily: fonts.sansSemiBold, fontSize: 10, letterSpacing: 1.2, lineHeight: 14 },
  status: { flexDirection: "row", alignItems: "center", gap: 6, paddingTop: 2 },
  dot: { width: 6, height: 6, borderRadius: 3 },
  statusWord: { fontFamily: fonts.sansSemiBold, fontSize: 12, lineHeight: 16 },
  actions: { flexDirection: "row", gap: spacing.xs, alignItems: "center" },
  button: { minHeight: 44, minWidth: 44, paddingHorizontal: spacing.md, borderRadius: 999, borderWidth: 1, alignItems: "center", justifyContent: "center", alignSelf: "flex-start" },
  buttonLabel: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
});
