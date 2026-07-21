import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, spacing, type } from "../../config/theme";
import { timeAgo, type IssueRun, type TeamsAgent } from "../../lib/teamsData";

// Port of the web IssueRunsSection (issues/IssueRunsSection.tsx), minus the
// live-runs join (no /inbox/live-runs feed on mobile yet) and the "View run"
// link (no run-detail screen yet): one row per run — status · agent · when.
// The caller sorts newest-first (useTeamsIssue) and hides the section by not
// rendering it... but an empty list also self-hides here so callers can pass
// state through unconditionally.

function statusColor(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return colors.forest;
  if (s === "failed" || s === "error" || s === "cancelled") return colors.danger;
  // In-flight runs read as live, not neutral grey.
  if (s === "running" || s === "queued") return colors.forest;
  return colors.mute;
}

export function IssueRunsSection({
  runs,
  agents,
}: {
  runs: IssueRun[];
  agents: TeamsAgent[];
}) {
  if (runs.length === 0) return null;
  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");
  return (
    <View style={styles.section}>
      <Text style={styles.overline}>RUNS</Text>
      {runs.map((run) => {
        const when = run.startedAt ?? run.createdAt;
        return (
          <View key={run.runId} style={styles.row}>
            <Text style={[styles.status, { color: statusColor(run.status) }]}>
              {run.status}
            </Text>
            <Text style={styles.agent} numberOfLines={1}>
              {agentName(run.agentId)}
            </Text>
            {when ? <Text style={styles.when}>{timeAgo(when)}</Text> : null}
          </View>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  section: {
    gap: spacing.sm,
    paddingTop: spacing.lg,
    borderTopWidth: 1,
    borderTopColor: colors.edge,
  },
  overline: { ...type.overline },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.xs,
  },
  status: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 12,
    lineHeight: 16,
    textTransform: "capitalize",
  },
  agent: {
    flex: 1,
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  when: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 14,
    color: colors.mute,
  },
});
