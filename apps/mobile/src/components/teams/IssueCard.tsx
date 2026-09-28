import { SymbolView, type SFSymbol } from "expo-symbols";
import { memo } from "react";
import { Pressable, StyleSheet, Text, useColorScheme, View } from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import {
  STATUS_LABELS,
  priorityColor,
  statusColors,
  toBoardStatus,
  type AssigneeDisplay,
  type BoardIssue,
  type IssueLabel,
} from "../../lib/teamsData";
import { AssigneeAvatar } from "./AssigneeAvatar";

// ---------------------------------------------------------------------------
// Differentiated Teams board card (contract §3). Fixes the "it's just a list"
// complaint by rendering status + priority + labels + assignee + subtask count
// as instantly-scannable, color-coded chips — not plain rows.
// ---------------------------------------------------------------------------

const MAX_LABELS = 3;

/** Statuses that read as "active/decisive" get a FILLED dot; the rest a hollow
 * ring. This splits the collision-prone pairs (in_progress filled vs todo
 * hollow; done filled vs backlog hollow) per the contract's §3 guidance. */
const FILLED_STATUSES = new Set(["in_progress", "done", "blocked"]);

/** SF Symbol per priority (contract §3 lucide → nearest SF Symbol). */
const PRIORITY_ICON: Record<string, SFSymbol> = {
  critical: "exclamationmark.triangle",
  high: "arrow.up",
  medium: "minus",
  low: "arrow.down",
};

// Status dot / chip / priority use SHEETS.light for their STRUCTURAL styling
// (border width, size, radius — identical in both schemes), but their COLORS
// come from statusColors(status, scheme): the light `colors.status` accents
// measure ~2.2-2.9:1 against a dark card (see STATUS_DARK in teamsData.ts),
// so the scheme is threaded through as a prop from IssueCardBase, the one
// place that already calls useColorScheme().
// (SHEETS is defined further below; referenced only inside function bodies,
// which run after the whole module has finished evaluating.)

function StatusDot({
  status,
  scheme,
}: {
  status: string | null | undefined;
  scheme: "light" | "dark";
}) {
  const { color } = statusColors(status, scheme);
  const key = toBoardStatus(status);
  const filled = key != null && FILLED_STATUSES.has(key);
  return (
    <View
      style={[
        SHEETS.light.statusDot,
        { borderColor: color },
        filled ? { backgroundColor: color } : { backgroundColor: "transparent" },
      ]}
    />
  );
}

function StatusChip({
  status,
  scheme,
}: {
  status: string | null | undefined;
  scheme: "light" | "dark";
}) {
  const { color, tint } = statusColors(status, scheme);
  const key = toBoardStatus(status);
  const label = key ? STATUS_LABELS[key] : status ? status.replace(/_/g, " ") : "No status";
  const cancelled = key === "cancelled";
  return (
    <View style={[SHEETS.light.statusChip, { backgroundColor: tint }]}>
      <StatusDot status={status} scheme={scheme} />
      <Text
        style={[
          SHEETS.light.statusChipText,
          { color },
          cancelled && SHEETS.light.cancelledText,
        ]}
        numberOfLines={1}
      >
        {label}
      </Text>
    </View>
  );
}

function PriorityIndicator({
  priority,
  scheme,
}: {
  priority: string | null | undefined;
  scheme: "light" | "dark";
}) {
  const color = priorityColor(priority, scheme);
  if (!color || !priority) return null;
  const icon = PRIORITY_ICON[priority];
  if (!icon) return null;
  return (
    <View style={SHEETS.light.priority}>
      <SymbolView accessibilityElementsHidden name={icon} size={13} tintColor={color} />
    </View>
  );
}

/** A hex color usable for a runtime-composed pill (server-supplied #rrggbb). */
function isHex6(value: string | null | undefined): value is string {
  return typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value);
}

function LabelChips({ labels }: { labels: IssueLabel[] }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const shown = labels.slice(0, MAX_LABELS);
  const extra = labels.length - shown.length;
  return (
    <View style={styles.labelRow}>
      {shown.map((label) => {
        const hex = isHex6(label.color) ? label.color : null;
        return (
          <View
            key={label.id}
            style={[
              styles.labelChip,
              hex
                ? { borderColor: hex, backgroundColor: `${hex}22` }
                : styles.labelChipNeutral,
            ]}
          >
            <Text
              // The server's label colour is the accent (border + tint above)
              // only — its own defaults (#2F5D50, #8C5A3C, …) are dark and
              // read ~2-3:1 as TEXT on the dark scheme's card surface, so the
              // label itself always renders in a palette foreground instead.
              style={[styles.labelChipText, { color: hex ? palette.fg : palette.fgMute }]}
              numberOfLines={1}
            >
              {label.name}
            </Text>
          </View>
        );
      })}
      {extra > 0 ? (
        <View style={[styles.labelChip, styles.labelChipNeutral]}>
          <Text style={[styles.labelChipText, { color: palette.fgMute }]}>+{extra}</Text>
        </View>
      ) : null}
    </View>
  );
}

export interface IssueCardProps {
  issue: BoardIssue;
  /** Resolved assignee (agent monogram or human initials/photo), or null. */
  assignee: AssigneeDisplay | null;
  /** Direct sub-issue count (derived from the board), 0 to hide. */
  subtaskCount: number;
  onPress: () => void;
  onMove: () => void;
}

function IssueCardBase({
  issue,
  assignee,
  subtaskCount,
  onPress,
  onMove,
}: IssueCardProps) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const { color } = statusColors(issue.status, scheme);
  const identifier = issue.identifier ?? issue.id.slice(0, 8);
  const labels = issue.labels ?? [];

  return (
    <Pressable
      style={({ pressed }) => [styles.card, pressed && styles.cardPressed]}
      onPress={onPress}
      onLongPress={onMove}
      delayLongPress={250}
      accessibilityRole="button"
      accessibilityLabel={`${issue.title}. ${identifier}. Long-press to change status.`}
    >
      {/* Leading status bar — a second, glanceable status encoding. */}
      <View style={[styles.statusBar, { backgroundColor: color }]} />

      <View style={styles.body}>
        {/* Top row: status chip + priority, with a move affordance on the right. */}
        <View style={styles.topRow}>
          <StatusChip status={issue.status} scheme={scheme} />
          <View style={styles.topRight}>
            <PriorityIndicator priority={issue.priority} scheme={scheme} />
            <Pressable
              onPress={onMove}
              hitSlop={8}
              accessibilityRole="button"
              accessibilityLabel="Change status"
              style={({ pressed }) => [styles.moveBtn, pressed && styles.moveBtnPressed]}
            >
              <SymbolView accessibilityElementsHidden name="ellipsis" size={16} tintColor={palette.fgMute} />
            </Pressable>
          </View>
        </View>

        {/* Identifier + title. */}
        <Text style={styles.identifier}>{identifier}</Text>
        <Text style={styles.title} numberOfLines={2}>
          {issue.title}
        </Text>

        {/* Labels. */}
        {labels.length > 0 ? <LabelChips labels={labels} /> : null}

        {/* Footer: assignee avatar/name + subtask count. */}
        {(assignee != null || subtaskCount > 0) && (
          <View style={styles.footer}>
            {assignee != null ? (
              <View style={styles.assignee}>
                <AssigneeAvatar assignee={assignee} />
                <Text style={styles.assigneeName} numberOfLines={1}>
                  {assignee.name}
                </Text>
              </View>
            ) : (
              <View />
            )}
            {subtaskCount > 0 ? (
              <Text style={styles.subtasks}>
                {subtaskCount} {subtaskCount === 1 ? "subtask" : "subtasks"}
              </Text>
            ) : null}
          </View>
        )}
      </View>
    </Pressable>
  );
}

export const IssueCard = memo(IssueCardBase);

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    card: {
      flexDirection: "row",
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      overflow: "hidden",
    },
    cardPressed: { backgroundColor: p.selected },
    statusBar: { width: 4, alignSelf: "stretch" },
    body: { flex: 1, padding: spacing.md, gap: spacing.sm },

    topRow: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: spacing.sm,
    },
    topRight: { flexDirection: "row", alignItems: "center", gap: spacing.sm },

    statusChip: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.xs,
      paddingHorizontal: spacing.sm,
      paddingVertical: spacing.xs,
      borderRadius: radii.sm,
      flexShrink: 1,
    },
    statusChipText: {
      fontFamily: fonts.sansMedium,
      fontSize: 11,
      lineHeight: 14,
      flexShrink: 1,
    },
    cancelledText: { textDecorationLine: "line-through", opacity: 0.75 },
    statusDot: {
      width: 8,
      height: 8,
      borderRadius: 4,
      borderWidth: 1.5,
    },

    priority: { alignItems: "center", justifyContent: "center" },
    moveBtn: { padding: spacing.xs, borderRadius: radii.sm },
    moveBtnPressed: { backgroundColor: p.raised },

    identifier: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      letterSpacing: 0.8,
      color: p.fgMute,
    },
    title: {
      fontFamily: fonts.sansMedium,
      fontSize: 14,
      lineHeight: 19,
      color: p.fg,
    },

    labelRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs },
    labelChip: {
      borderWidth: 1,
      borderRadius: radii.xs,
      paddingHorizontal: spacing.sm,
      paddingVertical: 2,
      maxWidth: 140,
    },
    labelChipNeutral: {
      borderColor: p.edge,
      backgroundColor: p.raised,
    },
    labelChipText: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
    },

    footer: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: spacing.sm,
      marginTop: spacing.xs,
    },
    assignee: { flexDirection: "row", alignItems: "center", gap: spacing.xs, flexShrink: 1 },
    assigneeName: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
      flexShrink: 1,
    },
    subtasks: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
