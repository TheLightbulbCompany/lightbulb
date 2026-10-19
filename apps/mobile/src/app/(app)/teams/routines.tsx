import { SymbolView } from "../../../components/ui/SymbolView";
import { useState } from "react";
import {
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  StyleSheet,
  Switch,
  Text,
  useColorScheme,
  View,
} from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
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
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
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
          accessibilityLabel="Run on thelightbulb.company (this routine needs inputs)"
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
        trackColor={{ true: palette.live, false: palette.edge }}
        accessibilityLabel={`${routine.title ?? "Routine"} ${enabled ? "active" : "paused"}`}
      />
    </View>
  );
}

export default function RoutinesScreen() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
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
        <SymbolView
          accessibilityElementsHidden
          name={isSubscription ? "lock" : "network.slash"}
          size={28}
          tintColor={palette.fgMute}
        />
        <Text style={styles.stateTitle}>
          {state.gate === "provisioning"
            ? "Setting up your workspace…"
            : isSubscription
              ? "Routines aren’t available for this workspace right now."
              : "Couldn’t reach your workspace"}
        </Text>
        {state.message ? <Text style={styles.stateDetail}>{state.message}</Text> : null}
        {!isSubscription && (
          <Pressable
            style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
            onPress={() => void retry()}
            accessibilityRole="button"
            accessibilityLabel="Retry"
          >
            <Text style={styles.buttonText}>Retry</Text>
          </Pressable>
        )}
      </View>
    );
  }

  const empty = state.routines.length === 0;
  return (
    <FlatList
      contentInsetAdjustmentBehavior="automatic"
      style={styles.page}
      data={state.routines}
      keyExtractor={(r) => r.id}
      contentContainerStyle={[styles.listContent, empty && styles.grow]}
      refreshControl={
        <RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={palette.fgMute} />
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
          <SymbolView accessibilityElementsHidden name="repeat" size={28} tintColor={palette.fgMute} />
          <Text style={styles.stateTitle}>No routines yet</Text>
          <Text style={styles.stateDetail}>
            Create scheduled automations for your agents on thelightbulb.company.
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

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
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
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.sm,
      marginBottom: spacing.sm,
    },
    sectionCount: { color: p.fgMute },
    stateTitle: {
      fontFamily: fonts.sansMedium,
      fontSize: 15,
      lineHeight: 20,
      color: p.fg,
      textAlign: "center",
    },
    stateDetail: {
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 19,
      color: p.fgMute,
      textAlign: "center",
    },
    button: {
      marginTop: spacing.xs,
      borderWidth: 1,
      borderColor: p.edge,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.lg,
      paddingVertical: spacing.sm,
    },
    buttonPressed: { opacity: 0.6 },
    buttonText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },

    row: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.md,
      paddingVertical: spacing.md,
      paddingHorizontal: spacing.sm,
      borderBottomWidth: 1,
      borderBottomColor: p.edge,
    },
    rowText: { flex: 1, gap: 3 },
    rowTitle: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    rowMeta: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
    rowStatus: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.warn,
      textTransform: "capitalize",
    },
    runAction: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fg,
    },
    runActionMuted: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
