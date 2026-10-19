import { SymbolView } from "../ui/SymbolView";
import { useState } from "react";
import {
  Animated,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { usePulse } from "./usePulse";

export type ToolPillStatus = "running" | "done" | "error";

/**
 * Tool-call pill (web design spec §3): a compact status chip for each tool
 * invocation in the transcript — leading status dot (pulsing while the tool
 * runs), 12px medium label, trailing chevron. Pressing toggles an expansion
 * panel with the tool's input args and, once tool_end/tool_error lands, its
 * result text. Status trios come verbatim from `colors.tool`.
 */
export function ToolPill({
  tool,
  status,
  args,
  result,
}: {
  tool: string;
  status: ToolPillStatus;
  args?: Record<string, unknown>;
  result?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const trio = colors.tool[status];
  const pulse = usePulse(status === "running", 0.3, 700);
  const label = status === "error" ? `${tool} · failed` : tool;
  const inputText =
    args && Object.keys(args).length > 0
      ? JSON.stringify(args, null, 2)
      : undefined;
  const hasResult = result != null && result.length > 0;
  const resultLabel = status === "error" ? "error" : "output";

  return (
    <View style={styles.wrap}>
      <Pressable
        style={[
          styles.pill,
          { backgroundColor: trio.bg, borderColor: trio.border },
        ]}
        onPress={() => setExpanded((e) => !e)}
        accessibilityRole="button"
        accessibilityState={{ expanded }}
        accessibilityLabel={`${label} tool call`}
      >
        <Animated.View
          style={[styles.dot, { backgroundColor: trio.dot, opacity: pulse }]}
        />
        <Text style={[styles.label, { color: trio.text }]} numberOfLines={1}>
          {label}
        </Text>
        <SymbolView
          accessibilityElementsHidden
          name={expanded ? "chevron.down" : "chevron.right"}
          size={12}
          tintColor={trio.text}
          style={styles.chevron}
        />
      </Pressable>
      {expanded && (
        <View style={styles.panel}>
          {inputText != null && (
            <View>
              <Text style={styles.sectionLabel}>input</Text>
              <ScrollView
                style={styles.pre}
                contentContainerStyle={styles.preContent}
                nestedScrollEnabled
              >
                <Text style={styles.preText}>{inputText}</Text>
              </ScrollView>
            </View>
          )}
          {hasResult && (
            <View>
              <Text style={styles.sectionLabel}>{resultLabel}</Text>
              <ScrollView
                style={styles.pre}
                contentContainerStyle={styles.preContent}
                nestedScrollEnabled
              >
                <Text style={styles.preText}>{result}</Text>
              </ScrollView>
            </View>
          )}
          {inputText == null && !hasResult && (
            <Text style={styles.emptyText}>No details for this call.</Text>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  // Pill hugs its content; the expansion panel stretches full width.
  wrap: { alignItems: "flex-start" },
  // Spec §3 verbatim: gap 6, px 10, py 4, radius 6, 1px border.
  pill: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    paddingVertical: spacing.xs,
    borderRadius: radii.sm,
    borderWidth: 1,
  },
  dot: { width: 6, height: 6, borderRadius: 3 },
  label: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    flexShrink: 1,
  },
  chevron: { opacity: 0.7 },
  // Expanded panel (§3): mt 6, radius 6, edge border, cream bg, padding 8.
  panel: {
    alignSelf: "stretch",
    marginTop: 6,
    borderRadius: radii.sm,
    borderWidth: 1,
    borderColor: colors.edge,
    backgroundColor: colors.cream,
    padding: spacing.sm,
    gap: spacing.sm,
  },
  sectionLabel: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.assistantInk,
    marginBottom: spacing.xs,
  },
  // Mono pre-style block: assistantInk on creamDeeper, radius 4, px 8 py 4,
  // capped at 192 and scrollable within the transcript list.
  pre: {
    maxHeight: 192,
    backgroundColor: colors.creamDeeper,
    borderRadius: radii.xs,
  },
  preContent: {
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.xs,
  },
  preText: {
    fontFamily: fonts.mono,
    fontSize: 12,
    lineHeight: 18,
    color: colors.assistantInk,
  },
  emptyText: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
});
