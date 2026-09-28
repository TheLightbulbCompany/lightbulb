import { SymbolView } from "expo-symbols";
import { router } from "expo-router";
import { Pressable, StyleSheet, Text, useColorScheme, View } from "react-native";

import { chatPalette, fonts, spacing, type ChatPalette } from "../../config/theme";
import { timeAgo, type IssueRun, type TeamsAgent } from "../../lib/teamsData";

// Port of the web IssueRunsSection (issues/IssueRunsSection.tsx), minus the
// live-runs join (no /inbox/live-runs feed on mobile yet). Each row is
// tappable → the run-detail screen (status · agent · when · chevron); failed
// rows surface the error snippet inline so a failure is inspectable at a
// glance. An empty list self-hides so callers can pass state through
// unconditionally.

function statusColor(status: string | null | undefined, p: ChatPalette): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return p.fg;
  if (s === "failed" || s === "error" || s === "cancelled") return p.alert;
  // In-flight runs read as live, not neutral grey.
  if (s === "running" || s === "queued") return p.fg;
  return p.fgMute;
}

export function IssueRunsSection({
  runs,
  agents,
}: {
  runs: IssueRun[];
  agents: TeamsAgent[];
}) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  if (runs.length === 0) return null;
  const agentName = (id?: string | null) =>
    agents.find((a) => a.id === id)?.name ?? (id ?? "Agent");
  return (
    <View style={styles.section}>
      <Text style={styles.overline}>RUNS</Text>
      {runs.map((run) => {
        const when = run.startedAt ?? run.createdAt;
        const errorSnippet =
          (run.status ?? "").toLowerCase() === "failed"
            ? run.error ?? run.errorCode ?? null
            : null;
        return (
          <Pressable
            key={run.runId}
            style={({ pressed }) => [styles.rowOuter, pressed && styles.rowPressed]}
            onPress={() =>
              router.push({
                pathname: "/(app)/teams/run/[runId]",
                params: { runId: run.runId },
              })
            }
            accessibilityRole="button"
            accessibilityLabel={`View run — ${run.status}`}
          >
            <View style={styles.row}>
              <Text style={[styles.status, { color: statusColor(run.status, palette) }]}>
                {run.status}
              </Text>
              <Text style={styles.agent} numberOfLines={1}>
                {agentName(run.agentId)}
              </Text>
              {when ? <Text style={styles.when}>{timeAgo(when)}</Text> : null}
              <SymbolView accessibilityElementsHidden name="chevron.right" size={14} tintColor={palette.fgMute} />
            </View>
            {errorSnippet ? (
              <Text style={styles.error} numberOfLines={2}>
                {errorSnippet}
              </Text>
            ) : null}
          </Pressable>
        );
      })}
    </View>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    section: {
      gap: spacing.sm,
      paddingTop: spacing.lg,
      borderTopWidth: 1,
      borderTopColor: p.edge,
    },
    overline: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
    },
    rowOuter: { paddingVertical: spacing.xs, gap: 2 },
    rowPressed: { opacity: 0.6 },
    row: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
    },
    status: {
      fontFamily: fonts.sansMedium,
      fontSize: 12,
      lineHeight: 16,
      textTransform: "capitalize",
    },
    agent: {
      flex: 1,
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    when: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 14,
      color: p.fgMute,
    },
    error: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.alert,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
