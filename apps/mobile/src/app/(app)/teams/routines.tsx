import Feather from "@expo/vector-icons/Feather";
import { useState } from "react";
import {
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  StyleSheet,
  Switch,
  Text,
  View,
} from "react-native";

import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import {
  routineCron,
  timeAgo,
  useTeamsRoutines,
  type TeamsAgent,
  type TeamsRoutine,
} from "../../../lib/teamsData";
import { teamsPanelUrl } from "../../../lib/webLinks";

// Routines (web RoutinesPanel parity, list-level controls): the operational
// levers — pause/resume a misfiring automation, fire one now — live here;
// create/edit stays on web (deep-link), matching the control-surface pattern.

function routineEnabled(routine: TeamsRoutine): boolean {
  return routine.status === "active";
}

function RoutineRow({
  routine,
  agents,
  onToggle,
  onRunNow,
}: {
  routine: TeamsRoutine;
  agents: TeamsAgent[];
  onToggle: (id: string, enabled: boolean) => Promise<void>;
  onRunNow: (routine: TeamsRoutine) => Promise<boolean>;
}) {
  const [runPhase, setRunPhase] = useState<"idle" | "running" | "done">("idle");
  // Disable the switch while its PATCH settles — the hook's ref-latch already
  // blocks a duplicate write, but an enabled switch that "snaps back" reads
  // as an ignored tap.
  const [toggling, setToggling] = useState(false);
  const enabled = routineEnabled(routine);
  const agentName =
    agents.find((a) => a.id === routine.assigneeAgentId)?.name ?? "Unassigned";
  const cron = routineCron(routine);
  // Run-now with declared variables needs the web's variables editor; mobile
  // defers those runs to web rather than firing with missing inputs.
  const needsVariables = (routine.variables ?? []).length > 0;
  const meta = [
    agentName,
    cron,
    routine.lastTriggeredAt ? `last ${timeAgo(routine.lastTriggeredAt)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <View style={styles.row}>
      <View style={styles.rowText}>
        <Text style={styles.rowTitle} numberOfLines={1}>
          {routine.title ?? "Untitled routine"}
        </Text>
        <Text style={styles.rowMeta} numberOfLines={1}>
          {meta}
        </Text>
        {routine.status && routine.status !== "active" && routine.status !== "paused" ? (
          <Text style={styles.rowStatus}>{routine.status}</Text>
        ) : null}
      </View>
      {needsVariables ? (
        <Pressable
          onPress={() => void Linking.openURL(teamsPanelUrl("routines"))}
          hitSlop={6}
          accessibilityRole="button"
          accessibilityLabel="Run on flickmeon.ai (this routine needs inputs)"
        >
          <Text style={styles.runActionMuted}>Run on web</Text>
        </Pressable>
      ) : (
        <Pressable
          onPress={() => {
            if (runPhase !== "idle") return;
            setRunPhase("running");
            void onRunNow(routine).then((ok) => {
              setRunPhase(ok ? "done" : "idle");
              if (ok) setTimeout(() => setRunPhase("idle"), 2500);
            });
          }}
          hitSlop={6}
          disabled={runPhase !== "idle"}
          accessibilityRole="button"
          accessibilityLabel={`Run ${routine.title ?? "routine"} now`}
        >
          <Text style={[styles.runAction, runPhase !== "idle" && styles.runActionMuted]}>
            {runPhase === "running" ? "Starting…" : runPhase === "done" ? "Started" : "Run now"}
          </Text>
        </Pressable>
      )}
      <Switch
        value={enabled}
        disabled={toggling}
        onValueChange={(next) => {
          setToggling(true);
          void onToggle(routine.id, next).finally(() => setToggling(false));
        }}
        trackColor={{ true: colors.forest, false: colors.creamDeepest }}
        accessibilityLabel={`${routine.title ?? "Routine"} ${enabled ? "active" : "paused"}`}
      />
    </View>
  );
}

export default function RoutinesScreen() {
  const { state, refreshing, retry, refresh, setEnabled, runNow } = useTeamsRoutines();

  if (state.kind === "loading") {
    return (
      <View style={[styles.page, styles.centered]}>
        <Text style={styles.stateDetail}>Loading routines…</Text>
      </View>
    );
  }

  if (state.kind === "gate") {
    const isSubscription = state.gate === "needs_subscription";
    return (
      <View style={[styles.page, styles.centered]}>
        <Feather
          name={isSubscription ? "lock" : "cloud-off"}
          size={28}
          color={colors.mute}
        />
        <Text style={styles.stateTitle}>
          {state.gate === "provisioning"
            ? "Setting up your workspace…"
            : isSubscription
              ? "Routines require an active subscription"
              : "Couldn’t reach your workspace"}
        </Text>
        {state.message ? <Text style={styles.stateDetail}>{state.message}</Text> : null}
        <Pressable
          style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
          onPress={
            isSubscription ? () => void Linking.openURL(teamsPanelUrl("routines")) : () => void retry()
          }
          accessibilityRole="button"
          accessibilityLabel={isSubscription ? "Manage on flickmeon.ai" : "Retry"}
        >
          <Text style={styles.buttonText}>
            {isSubscription ? "Manage on flickmeon.ai" : "Retry"}
          </Text>
        </Pressable>
      </View>
    );
  }

  const empty = state.routines.length === 0;
  return (
    <FlatList
      style={styles.page}
      data={state.routines}
      keyExtractor={(r) => r.id}
      contentContainerStyle={[styles.listContent, empty && styles.grow]}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={colors.mute} />
      }
      ListHeaderComponent={
        !empty ? (
          <Text style={styles.sectionLabel}>
            ROUTINES
            <Text style={styles.sectionCount}> · {state.routines.length}</Text>
          </Text>
        ) : null
      }
      ListEmptyComponent={
        <View style={styles.centered}>
          <Feather name="repeat" size={28} color={colors.mute} />
          <Text style={styles.stateTitle}>No routines yet</Text>
          <Text style={styles.stateDetail}>
            Create scheduled automations for your agents on flickmeon.ai.
          </Text>
        </View>
      }
      renderItem={({ item }) => (
        <RoutineRow
          routine={item}
          agents={state.agents}
          onToggle={setEnabled}
          onRunNow={(routine) => runNow(routine.id)}
        />
      )}
    />
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  centered: {
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    padding: spacing.xl,
    flexGrow: 1,
  },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
  },
  grow: { flexGrow: 1, justifyContent: "center" },
  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },
  stateTitle: {
    fontFamily: fonts.sansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    textAlign: "center",
  },
  stateDetail: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 19,
    color: colors.mute,
    textAlign: "center",
  },
  button: {
    marginTop: spacing.xs,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  buttonPressed: { opacity: 0.6 },
  buttonText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },

  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: colors.edge,
  },
  rowText: { flex: 1, gap: 3 },
  rowTitle: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  rowMeta: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  rowStatus: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.warn,
    textTransform: "capitalize",
  },
  runAction: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.forest,
  },
  runActionMuted: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
});
