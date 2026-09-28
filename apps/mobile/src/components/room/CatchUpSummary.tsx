import Feather from "@expo/vector-icons/Feather";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { useCallback, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
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
  generateSummary?: (
    messages: CanonicalRoomMessage[],
    readSeq: number | null,
  ) => Summary | Promise<Summary>;
}

function Section({ title, items, empty, palette }: { title: string; items: string[]; empty: string; palette: ChatPalette }) {
  return (
    <View style={styles.section}>
      <Text style={[styles.sectionTitle, { color: palette.fg }]}>{title}</Text>
      {items.length ? items.map((item) => (
        <Text key={`${title}-${item}`} style={[styles.item, { color: palette.fg }]}>• {item}</Text>
      )) : (
        <Text style={[styles.emptySection, { color: palette.fgMute }]}>{empty}</Text>
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
  const palette = useChatPalette();
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
          style={[styles.action, { borderColor: palette.edge, backgroundColor: palette.surface }]}
        >
          <Feather name="file-text" size={15} color={palette.link} />
          <Text style={[styles.actionText, { color: palette.link }]}>Summary</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={[styles.card, { borderColor: palette.edge, backgroundColor: palette.surface }]} accessibilityLabel="Conversation summary">
      <View style={styles.cardHeader}>
        <View>
          <Text style={[styles.title, { color: palette.fg }]}>Catch-up summary</Text>
          <Text style={[styles.scope, { color: palette.fgMute }]}>
            {state.kind === "ready" ? state.summary.scope : "Preparing a summary of this chat"}
          </Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Dismiss summary"
          hitSlop={10}
          onPress={() => setState({ kind: "closed" })}
        >
          <Feather name="x" size={20} color={palette.fg} />
        </Pressable>
      </View>

      {offline ? (
        <Text style={[styles.offline, { color: palette.fgMute }]}>
          Offline · using messages already loaded on this device.
        </Text>
      ) : null}

      {state.kind === "loading" ? (
        <View style={styles.loading} accessibilityRole="progressbar">
          <ActivityIndicator color={palette.link} />
          <Text style={[styles.loadingText, { color: palette.fgMute }]}>Summarizing…</Text>
        </View>
      ) : null}

      {state.kind === "error" ? (
        <View accessibilityRole="alert" style={styles.failure}>
          <Text style={[styles.failureText, { color: palette.alert }]}>Couldn’t generate the summary.</Text>
          <Pressable accessibilityRole="button" accessibilityLabel="Retry summary" onPress={generate}>
            <Text style={[styles.retry, { color: palette.link }]}>Try again</Text>
          </Pressable>
        </View>
      ) : null}

      {state.kind === "ready" && state.summary.messageCount === 0 ? (
        <Text style={[styles.empty, { color: palette.fgMute }]}>You’re caught up — there are no new messages to summarize.</Text>
      ) : null}

      {state.kind === "ready" && state.summary.messageCount > 0 ? (
        <View style={styles.sections}>
          <Section
            title="Main discussion"
            items={state.summary.mainDiscussion}
            empty="No text discussion found."
            palette={palette}
          />
          <Section title="Decisions" items={state.summary.decisions} empty="No decisions were stated." palette={palette} />
          <Section title="Action items" items={state.summary.actionItems} empty="No action items were stated." palette={palette} />
          <Section title="Open questions" items={state.summary.openQuestions} empty="No open questions were found." palette={palette} />
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
    borderRadius: 18,
    paddingHorizontal: spacing.md,
  },
  actionText: { fontFamily: fonts.sansSemiBold, fontSize: 14 },
  card: {
    marginHorizontal: spacing.md,
    marginTop: spacing.sm,
    padding: spacing.md,
    gap: spacing.sm,
    borderWidth: 1,
    borderRadius: radii.md,
  },
  cardHeader: { flexDirection: "row", justifyContent: "space-between", gap: spacing.md },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 16, lineHeight: 22 },
  scope: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 17 },
  offline: { fontFamily: fonts.sansMedium, fontSize: 12 },
  loading: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.md,
  },
  loadingText: { fontFamily: fonts.sans, fontSize: 14 },
  failure: { gap: spacing.xs },
  failureText: { fontFamily: fonts.sans, fontSize: 14 },
  retry: { fontFamily: fonts.sansSemiBold, fontSize: 14 },
  empty: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  sections: { gap: spacing.md },
  section: { gap: 3 },
  sectionTitle: { fontFamily: fonts.sansSemiBold, fontSize: 13 },
  item: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  emptySection: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
