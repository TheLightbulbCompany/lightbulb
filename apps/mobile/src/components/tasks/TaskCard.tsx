// Thread cards under a Lightbulb message (the phone twin of web's components/tasks/TaskCard.tsx):
// the whole card opens the thread, the one action sits inside it, state colour marks the word and
// a dot, never a fill. 44pt targets.

import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { router } from "expo-router";
import {
  putTaskSummary, specialistLabel, summaryView, taskSummaryLine, useTaskCache, useTaskList, type CanonicalRoomMessage, type TaskRef, type TaskSummary, type TaskTone,
} from "@lightbulb/shared";

import { fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { ACTION_LABELS, errorCopy, useTaskActions } from "./useTaskActions";

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

export function TaskCard({ task, taskId, viewerId, palette, onOpen = openTaskScreen, hideAction, children }: {
  task: TaskSummary | undefined; taskId: string; viewerId: string; palette: ChatPalette;
  onOpen?: (taskId: string) => void; hideAction?: boolean; children?: React.ReactNode;
}) {
  const { run } = useTaskActions();
  const { agentNames } = useLiveEvents();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!task) {
    return (
      <View style={[styles.card, styles.unavailable, { borderColor: palette.edge }]} testID={`task-card-${taskId}`}>
        <Text style={[styles.meta, { color: palette.fgMute }]}>{"This thread isn't available"}</Text>
      </View>
    );
  }
  const view = summaryView(task, viewerId);
  const inline = !hideAction && view.action && view.action !== "answer" && view.action !== "review" ? view.action : null;
  const line = taskSummaryLine(task, viewerId, agentNames);
  const detail = line === view.word || line.startsWith(`${view.word} ·`) ? null : line;
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Open ${task.title}, ${view.word}`}
      onPress={() => onOpen(taskId)}
      style={({ pressed }) => [styles.card, { borderColor: palette.edge, backgroundColor: pressed ? palette.hover : palette.surface }]}
      testID={`task-card-${taskId}`}
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
      {children}
      {inline ? (
        <ActionButton label={ACTION_LABELS[inline]} palette={palette} primary={view.tone === "waiting" || view.tone === "review"} disabled={pending}
          onPress={async () => {
            setPending(true);
            setError(null);
            const res = await run(task, inline);
            setPending(false);
            if (!res.ok) setError(res.message);
          }} />
      ) : null}
      {error ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{error}</Text> : null}
    </Pressable>
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

/** Thread cards under a Lightbulb message (`taskRefs`), with the Suggested block. One shared room read. */
export function MessageTasks({ message, viewerId, palette, onOpen }: {
  message: CanonicalRoomMessage; viewerId: string; palette: ChatPalette; onOpen?: (taskId: string) => void;
}) {
  const { tasks } = useLiveEvents();
  const refs = message.taskRefs ?? [];
  const { data } = useTaskList(tasks, refs.length ? { kind: "room", roomId: message.roomId } : null);
  if (!refs.length || !data) return null;
  const byId = new Map(data.tasks.map((t) => [t.taskId, t]));
  const started = refs.filter((r) => r.role === "started");
  const suggested = refs.filter((r) => r.role === "suggested");
  return (
    <View style={styles.list} accessibilityLabel="Threads in this message">
      {started.map((r) => <TaskCard key={r.taskId} task={byId.get(r.taskId)} taskId={r.taskId} viewerId={viewerId} palette={palette} onOpen={onOpen} />)}
      {suggested.length > 0 ? <SuggestedBlock refs={suggested} taskById={byId} viewerId={viewerId} palette={palette} onOpen={onOpen} /> : null}
    </View>
  );
}

export function SuggestedBlock({ refs, taskById, viewerId, palette, onOpen }: {
  refs: TaskRef[]; taskById: ReadonlyMap<string, TaskSummary>; viewerId: string; palette: ChatPalette; onOpen?: (taskId: string) => void;
}) {
  const { tasks } = useLiveEvents();
  const { act, refetch } = useTaskActions();
  const { cache, mutate } = useTaskCache();
  const [pending, setPending] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const proposed = refs.map((r) => taskById.get(r.taskId)).filter((t): t is TaskSummary => t?.status === "proposed");
  const one = async (task: TaskSummary, verb: "start" | "dismiss") => {
    setPending(task.taskId);
    const res = await act(task, verb);
    setPending(null);
    setErrors((e) => ({ ...e, [task.taskId]: res.ok ? "" : res.message }));
  };
  const all = async () => {
    setPending("all");
    try {
      const res = await tasks.startMany(proposed.map((t) => t.taskId));
      // Write the started summaries now: AppSync may drop their task.changed events.
      await Promise.all(res.tasks.map((t) => putTaskSummary(cache, mutate, t, tasks)));
      const started = new Set(res.tasks.map((t) => t.taskId));
      const failed = proposed.filter((t) => !started.has(t.taskId));
      await Promise.all(failed.map((t) => refetch(t.taskId)));
      setErrors(Object.fromEntries(failed.map((t) => [t.taskId, "Couldn't start this one. Try again."])));
    } catch (error) {
      setErrors(Object.fromEntries(proposed.map((t) => [t.taskId, errorCopy(error)])));
    } finally {
      setPending(null);
    }
  };
  return (
    <View accessibilityLabel="Suggested" style={styles.list}>
      <View style={styles.row}>
        <Text style={[styles.label, { color: palette.fgMute }]}>SUGGESTED</Text>
        {proposed.length >= 2 ? <ActionButton label="Start all" palette={palette} disabled={pending !== null} onPress={() => void all()} /> : null}
      </View>
      {refs.map((r) => {
        const task = taskById.get(r.taskId);
        const waiting = task?.status === "proposed";
        return (
          <TaskCard key={r.taskId} task={task} taskId={r.taskId} viewerId={viewerId} palette={palette} onOpen={onOpen} hideAction={waiting}>
            {waiting ? (
              <View style={styles.actions}>
                <ActionButton label="Start" palette={palette} primary disabled={pending !== null} onPress={() => void one(task, "start")} />
                <ActionButton label="Dismiss" palette={palette} disabled={pending !== null} onPress={() => void one(task, "dismiss")} />
              </View>
            ) : null}
            {errors[r.taskId] ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{errors[r.taskId]}</Text> : null}
          </TaskCard>
        );
      })}
    </View>
  );
}

/** "Sent to Workshop signup app ›" under a person's message (one thread opens it; several name the count). */
export function RoutedToLine({ message, palette, onOpen = openTaskScreen }: {
  message: CanonicalRoomMessage; palette: ChatPalette; onOpen?: (taskId: string) => void;
}) {
  const { tasks } = useLiveEvents();
  const routedTo = message.routedTo;
  const single = !!routedTo && (routedTo.passThrough || routedTo.taskIds.length === 1);
  const { data } = useTaskList(tasks, single ? { kind: "room", roomId: message.roomId } : null);
  if (!routedTo || !routedTo.taskIds.length) return null;
  const { taskIds, started, suggested } = routedTo;
  const label = single
    ? `Sent to ${data?.tasks.find((t) => t.taskId === taskIds[0])?.title ?? "a thread"}`
    : [`Sent to ${started} ${started === 1 ? "thread" : "threads"}`, suggested ? `${suggested} suggested` : ""].filter(Boolean).join(", ");
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
