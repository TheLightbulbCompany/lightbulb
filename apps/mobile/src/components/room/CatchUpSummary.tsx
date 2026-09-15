import Feather from "@expo/vector-icons/Feather";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { useCallback, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { buildCatchUpSummary, type CatchUpSummary as Summary } from "../../lib/catchUpSummary";

type SummaryState =
  | { kind: "closed" }
  | { kind: "loading" }
  | { kind: "ready"; summary: Summary }
  | { kind: "error" };

interface Props {
  messages: CanonicalRoomMessage[];
  readSeq: number | null;
  offline: boolean;
  generateSummary?: typeof buildCatchUpSummary;
}

function Section({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {items.length ? items.map((item) => (
        <Text key={`${title}-${item}`} style={styles.item}>• {item}</Text>
      )) : (
        <Text style={styles.emptySection}>{empty}</Text>
      )}
    </View>
  );
}

export function CatchUpSummary({
  messages,
  readSeq,
  offline,
  generateSummary = buildCatchUpSummary,
}: Props) {
  const [state, setState] = useState<SummaryState>({ kind: "closed" });

  const generate = useCallback(() => {
    setState({ kind: "loading" });
    void Promise.resolve()
      .then(() => generateSummary(messages, readSeq))
      .then((summary) => setState({ kind: "ready", summary }))
      .catch(() => setState({ kind: "error" }));
  }, [generateSummary, messages, readSeq]);

  if (state.kind === "closed") {
    return (
      <View style={styles.actionRow}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Summary"
          onPress={generate}
          style={styles.action}
        >
          <Feather name="file-text" size={15} color={colors.forest} />
          <Text style={styles.actionText}>Summary</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={styles.card} accessibilityLabel="Conversation summary">
      <View style={styles.cardHeader}>
        <View>
          <Text style={styles.title}>Catch-up summary</Text>
          <Text style={styles.scope}>
            {state.kind === "ready" ? state.summary.scope : "Preparing a summary of this chat"}
          </Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Dismiss summary"
          hitSlop={10}
          onPress={() => setState({ kind: "closed" })}
        >
          <Feather name="x" size={20} color={colors.ink} />
        </Pressable>
      </View>

      {offline ? (
        <Text style={styles.offline}>
          Offline · using messages already loaded on this device.
        </Text>
      ) : null}

      {state.kind === "loading" ? (
        <View style={styles.loading} accessibilityRole="progressbar">
          <ActivityIndicator color={colors.forest} />
          <Text style={styles.loadingText}>Summarizing…</Text>
        </View>
      ) : null}

      {state.kind === "error" ? (
        <View accessibilityRole="alert" style={styles.failure}>
          <Text style={styles.failureText}>Couldn’t generate the summary.</Text>
          <Pressable accessibilityRole="button" accessibilityLabel="Retry summary" onPress={generate}>
            <Text style={styles.retry}>Try again</Text>
          </Pressable>
        </View>
      ) : null}

      {state.kind === "ready" && state.summary.messageCount === 0 ? (
        <Text style={styles.empty}>You’re caught up — there are no new messages to summarize.</Text>
      ) : null}

      {state.kind === "ready" && state.summary.messageCount > 0 ? (
        <View style={styles.sections}>
          <Section
            title="Main discussion"
            items={state.summary.mainDiscussion}
            empty="No text discussion found."
          />
          <Section title="Decisions" items={state.summary.decisions} empty="No decisions were stated." />
          <Section title="Action items" items={state.summary.actionItems} empty="No action items were stated." />
          <Section title="Open questions" items={state.summary.openQuestions} empty="No open questions were found." />
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  actionRow: { paddingHorizontal: spacing.md, paddingTop: spacing.sm, alignItems: "flex-end" },
  action: {
    minHeight: 36,
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: 18,
    paddingHorizontal: spacing.md,
    backgroundColor: colors.white,
  },
  actionText: { fontFamily: fonts.sansSemiBold, fontSize: 14, color: colors.forest },
  card: {
    marginHorizontal: spacing.md,
    marginTop: spacing.sm,
    padding: spacing.md,
    gap: spacing.sm,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    backgroundColor: colors.white,
  },
  cardHeader: { flexDirection: "row", justifyContent: "space-between", gap: spacing.md },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 16, lineHeight: 22, color: colors.ink },
  scope: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 17, color: colors.mute },
  offline: { fontFamily: fonts.sansMedium, fontSize: 12, color: colors.mute },
  loading: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.md,
  },
  loadingText: { fontFamily: fonts.sans, fontSize: 14, color: colors.mute },
  failure: { gap: spacing.xs },
  failureText: { fontFamily: fonts.sans, fontSize: 14, color: colors.errorText },
  retry: { fontFamily: fonts.sansSemiBold, fontSize: 14, color: colors.forest },
  empty: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.mute },
  sections: { gap: spacing.md },
  section: { gap: 3 },
  sectionTitle: { fontFamily: fonts.sansSemiBold, fontSize: 13, color: colors.ink },
  item: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.ink },
  emptySection: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
});
