// The Work tab with the super-agent on (mockup step 3 on the phone): Overview · Scheduled. The overview
// groups the workspace's threads by the server's `shownGroup`; a row opens the task screen. Scheduled
// lists the automations; a row opens its detail.

import { useAuth } from "@clerk/expo";
import {
  automationStatusLine, deliveryWords, overviewGroups, scheduleWords, useAutomations, viewerTimeZone, summaryView, taskSummaryLine, useTaskList, type TaskScope, type TaskSummary,
} from "@lightbulb/shared";
import { router } from "expo-router";
import { useContext, useState } from "react";
import { HeaderHeightContext } from "expo-router/react-navigation";
import { Pressable, ScrollView, StyleSheet, Text, View, useColorScheme } from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { SegmentedControl } from "../ui/SegmentedControl";
import { useAutomationSource } from "./automations";
import { TaskStatus, openTaskScreen } from "./TaskCard";

type Segment = "overview" | "scheduled";
const SEGMENTS: readonly { key: Segment; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "scheduled", label: "Scheduled" },
];
const PLACEHOLDERS: Partial<Record<string, string>> = { waiting_on_you: "Nothing needs you", resolved: "Nothing resolved yet" };

export function WorkScreen({ scope = { kind: "workspace" }, onOpenOldBoard, viewerTz = viewerTimeZone() }: {
  scope?: TaskScope; onOpenOldBoard?: () => void; viewerTz?: string;
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const palette = chatPalette[scheme];
  const [segment, setSegment] = useState<Segment>("overview");
  const headerHeight = useContext(HeaderHeightContext) ?? 0;
  return (
    <View style={[styles.host, { paddingTop: headerHeight, backgroundColor: palette.ground }]} testID="work-host">
      <View style={styles.toggleBar}>
        <SegmentedControl label="Work view" segments={SEGMENTS} value={segment} onChange={setSegment} colorScheme={scheme} />
      </View>
      <ScrollView contentContainerStyle={styles.body} contentInsetAdjustmentBehavior="automatic">
        {segment === "overview" ? <OverviewList scope={scope} palette={palette} /> : <ScheduledList palette={palette} viewerTz={viewerTz} />}
        {onOpenOldBoard ? (
          <Pressable accessibilityRole="button" accessibilityLabel="Task board (old)" onPress={onOpenOldBoard} style={styles.oldLink} hitSlop={8}>
            <Text style={[styles.link, { color: palette.fgMute }]}>Task board (old) ›</Text>
          </Pressable>
        ) : null}
      </ScrollView>
    </View>
  );
}

export function OverviewList({ scope, palette, onOpen = openTaskScreen }: { scope: TaskScope; palette: ChatPalette; onOpen?: (taskId: string) => void }) {
  const { userId } = useAuth();
  const { tasks } = useLiveEvents();
  const { data, error, mutate } = useTaskList(tasks, scope);
  const [resolvedOpen, setResolvedOpen] = useState(false);
  if (!data) return <LoadState what="Threads" error={!!error} onRetry={() => void mutate()} palette={palette} />;
  return (
    <View style={styles.groups}>
      {overviewGroups(data.tasks).map((group) => {
        const collapsible = group.id === "resolved";
        const open = !collapsible || resolvedOpen;
        const heading = `${group.label} · ${group.tasks.length}`;
        return (
          <View key={group.id} style={styles.group}>
            {collapsible ? (
              <Pressable accessibilityRole="button" accessibilityLabel={heading} accessibilityState={{ expanded: open }} onPress={() => setResolvedOpen(!open)} hitSlop={8}>
                <Text style={[styles.heading, { color: palette.fgMute }]}>{heading}{open ? "" : " ›"}</Text>
              </Pressable>
            ) : (
              <Text accessibilityRole="header" style={[styles.heading, { color: palette.fgMute }]}>{heading}</Text>
            )}
            {open ? (group.tasks.length
              ? (
                <View style={[styles.list, { borderColor: palette.edge, backgroundColor: palette.surface }]}>
                  {group.tasks.map((t, i) => <OverviewRow key={t.taskId} task={t} viewerId={userId ?? ""} palette={palette} onOpen={onOpen} first={i === 0} />)}
                </View>
              )
              : PLACEHOLDERS[group.id] ? (
                <View style={[styles.placeholder, { borderColor: palette.edge }]}>
                  <Text style={[styles.meta, { color: palette.fgMute }]}>{PLACEHOLDERS[group.id]}</Text>
                </View>
              ) : null) : null}
          </View>
        );
      })}
    </View>
  );
}

function OverviewRow({ task, viewerId, palette, onOpen, first }: {
  task: TaskSummary; viewerId: string; palette: ChatPalette; onOpen: (taskId: string) => void; first: boolean;
}) {
  const view = summaryView(task, viewerId);
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={`Open ${task.title}, ${view.word}`} onPress={() => onOpen(task.taskId)}
      style={({ pressed }) => [styles.row, { backgroundColor: pressed ? palette.hover : "transparent", borderColor: palette.edge, borderTopWidth: first ? 0 : StyleSheet.hairlineWidth }]}>
      <View style={styles.rowMain}>
        <Text style={[styles.title, { color: palette.fg }]} numberOfLines={1}>{task.title}</Text>
        <Text style={[styles.meta, { color: palette.fgMute }]} numberOfLines={1}>{taskSummaryLine(task, viewerId)}</Text>
      </View>
      <TaskStatus task={task} viewerId={viewerId} palette={palette} />
    </Pressable>
  );
}

export function ScheduledList({ palette, viewerTz, onOpen = (id) => router.push({ pathname: "/(app)/automation/[id]", params: { id } }) }: {
  palette: ChatPalette; viewerTz: string; onOpen?: (id: string) => void;
}) {
  const { data, error, mutate } = useAutomations(useAutomationSource());
  if (!data) return <LoadState what="Schedules" error={!!error} onRetry={() => void mutate()} palette={palette} />;
  const rows = data.automations;
  if (!rows.length) {
    return (
      <View style={[styles.placeholder, { borderColor: palette.edge }]}>
        <Text style={[styles.meta, { color: palette.fgMute }]}>Ask @lightbulb to put recurring work on a schedule.</Text>
      </View>
    );
  }
  return (
    <View style={[styles.list, { borderColor: palette.edge, backgroundColor: palette.surface }]}>
      {rows.map((a, i) => (
        <Pressable key={a.id} accessibilityRole="button" accessibilityLabel={`Open ${a.name}`} onPress={() => onOpen(a.id)}
          style={({ pressed }) => [styles.row, { backgroundColor: pressed ? palette.hover : "transparent", borderColor: palette.edge, borderTopWidth: i === 0 ? 0 : StyleSheet.hairlineWidth }]}>
          <View style={styles.rowMain}>
            <Text style={[styles.title, { color: palette.fg }]} numberOfLines={1}>{a.name}</Text>
            <Text style={[styles.meta, { color: palette.fgMute }]} numberOfLines={2}>
              {[scheduleWords(a, viewerTz) ?? "No schedule", deliveryWords(a.deliverTo), automationStatusLine(a)].filter(Boolean).join(" · ")}
            </Text>
          </View>
        </Pressable>
      ))}
    </View>
  );
}

/** Loading, or a failed read with Retry (never the empty prompt while nothing is known). */
export function LoadState({ what, error, onRetry, palette }: { what: string; error: boolean; onRetry: () => void; palette: ChatPalette }) {
  if (!error) return <Text accessibilityRole="text" style={[styles.meta, { color: palette.fgMute }]}>Loading…</Text>;
  return (
    <View style={styles.loadRow}>
      <Text accessibilityRole="alert" style={[styles.meta, { color: palette.fgMute }]}>{what} couldn’t load.</Text>
      <Pressable accessibilityRole="button" accessibilityLabel={`Retry loading ${what.toLowerCase()}`} onPress={onRetry} hitSlop={8} style={styles.retry}>
        <Text style={[styles.link, { color: palette.link }]}>Retry</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  loadRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  retry: { minHeight: 44, justifyContent: "center" },
  host: { flex: 1 },
  toggleBar: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm },
  body: { paddingHorizontal: spacing.lg, paddingBottom: spacing.xl, gap: spacing.lg },
  groups: { gap: spacing.lg },
  group: { gap: spacing.xs },
  // Group labels read like the rest of the app's section labels (label-caps), so the rows carry the weight.
  heading: { fontFamily: fonts.sansSemiBold, fontSize: 10, letterSpacing: 1.2, lineHeight: 14, textTransform: "uppercase" },
  list: { borderWidth: 1, borderRadius: radii.md, overflow: "hidden" },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.sm, paddingHorizontal: spacing.md, paddingVertical: spacing.sm, minHeight: 44 },
  rowMain: { flex: 1, minWidth: 0, gap: 2 },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  placeholder: { borderWidth: 1, borderStyle: "dashed", borderRadius: radii.md, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  oldLink: { paddingVertical: spacing.sm, minHeight: 44, justifyContent: "center" },
  link: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
