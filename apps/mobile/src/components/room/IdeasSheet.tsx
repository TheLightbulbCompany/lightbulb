import { useAuth } from "@clerk/expo";
import { ideaEffortLine, ideaProgress, orderIdeas, useIdeas, type Idea } from "@lightbulb/shared";
import { SymbolView } from "expo-symbols";
import { useState } from "react";
import { ActionSheetIOS, ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { useLiveEvents } from "../../providers/live-events";
import { BottomSheet } from "../ui/BottomSheet";
import { Avatar } from "./Avatar";

type IdeasSheetProps = {
  visible: boolean;
  onClose: () => void;
  /** Opened from an agent's DM: that agent's ideas come first. */
  agentId?: string;
  /** Let's do it resolved: the DM where the person's message landed. */
  onStarted: (roomId: string) => void;
};

/**
 * Ideas on the phone, calm like Muse's Ideas tab (owner, 2026-10-15): every idea as one row (the
 * agent's avatar, a bold title, a few grey lines of why); a row opens its detail in the sheet
 * (what's included, the effort, Let's do it, and "…" → Not interested). Running ideas sit on
 * top (the starter's own open its DM); ideas this person is already running leave the list.
 * No prices (owner ruling and the App Store's).
 */
export function IdeasSheet({ visible, onClose, agentId, onStarted }: IdeasSheetProps) {
  const palette = useChatPalette();
  const { userId, orgId } = useAuth();
  const { messaging } = useLiveEvents();
  const { ideas, running, loading, error, start, dismiss } = useIdeas(messaging, visible && userId ? `${userId}:${orgId ?? userId}` : null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  // A start is per person: a teammate running an idea leaves it startable in your own DM.
  const runningIds = new Set(running.filter((r) => r.starterId === userId).map((r) => r.ideaId));
  const shown = orderIdeas(ideas.filter((idea) => !runningIds.has(idea.id)), agentId);
  const open = openId ? ideas.find((idea) => idea.id === openId) : undefined;

  const close = () => {
    setOpenId(null);
    onClose();
  };
  const letsDoIt = async (idea: Idea) => {
    setStarting(true);
    setStartError(null);
    try {
      const { roomId } = await start(idea.id);
      setOpenId(null);
      onStarted(roomId);
    } catch (err) {
      // The server says why (Work did not answer, already starting): show its words.
      setStartError(err instanceof Error && err.message ? err.message : "The idea could not start. Try again.");
    } finally {
      setStarting(false);
    }
  };
  const more = (idea: Idea) =>
    ActionSheetIOS.showActionSheetWithOptions(
      { options: ["Not interested", "Cancel"], cancelButtonIndex: 1, destructiveButtonIndex: 0 },
      (index) => {
        if (index !== 0) return;
        setOpenId(null);
        void dismiss(idea.id).catch(() => setStartError("The idea could not be hidden. Try again."));
      },
    );

  return (
    <BottomSheet visible={visible} onClose={close} large>
      <View style={[styles.sheet, { backgroundColor: palette.surface }]}>
        {startError || error ? (
          <Text accessibilityRole="alert" style={[styles.why, { color: palette.alert }]}>
            {startError ?? "Couldn't load ideas."}
          </Text>
        ) : null}
        {open ? (
          <ScrollView contentContainerStyle={styles.detail}>
            <Pressable accessibilityRole="button" accessibilityLabel="All ideas" hitSlop={8} onPress={() => setOpenId(null)} style={styles.back}>
              <SymbolView accessibilityElementsHidden name="chevron.left" size={14} tintColor={palette.fgSoft} />
              <Text style={[styles.why, { color: palette.fgSoft }]}>All ideas</Text>
            </Pressable>
            <Text accessibilityRole="header" style={[styles.detailTitle, { color: palette.fg }]}>{open.title}</Text>
            <Text style={[styles.body, { color: palette.fgSoft }]}>{open.why}</Text>
            <View style={[styles.included, { borderColor: palette.edge }]}>
              <Text style={[styles.title, { color: palette.fg }]}>What&apos;s included</Text>
              {open.deliverables.map((line) => (
                <View key={line} style={styles.check}>
                  <Text style={[styles.body, styles.checkText, { color: palette.fgSoft }]}>{line}</Text>
                  <SymbolView accessibilityElementsHidden name="checkmark.circle" size={18} tintColor={palette.fgMute} />
                </View>
              ))}
            </View>
            <Text style={[styles.why, { color: palette.fgSoft }]}>{ideaEffortLine(open)}</Text>
            <View style={styles.actions}>
              <Pressable accessibilityRole="button" accessibilityLabel="More options" onPress={() => more(open)}
                style={({ pressed }) => [styles.more, { borderColor: palette.edge }, pressed && styles.dim]}>
                <SymbolView accessibilityElementsHidden name="ellipsis" size={16} tintColor={palette.fg} />
              </Pressable>
              <Pressable accessibilityRole="button" accessibilityLabel="Let's do it" accessibilityState={{ disabled: starting }}
                disabled={starting} onPress={() => void letsDoIt(open)}
                style={({ pressed }) => [styles.go, { backgroundColor: palette.fg }, (pressed || starting) && styles.dim]}>
                <Text style={[styles.goText, { color: palette.onFg }]}>{starting ? "Starting…" : "Let's do it"}</Text>
              </Pressable>
            </View>
          </ScrollView>
        ) : (
          <>
            <Text accessibilityRole="header" style={[styles.heading, { color: palette.fg }]}>Ideas</Text>
            {loading ? <ActivityIndicator /> : null}
            <ScrollView>
              {running.length > 0 ? (
                <View style={styles.running}>
                  <Text style={[styles.section, { color: palette.fgMute }]}>Running</Text>
                  {running.map((item) => {
                    const mine = item.starterId === userId && !!item.roomId;
                    const progress = ideaProgress(item.progress);
                    const meta = [item.agentName, progress ? `${progress.done} of ${progress.total} tasks done` : "Planning",
                      mine ? null : `Started by ${item.starterName}`].filter(Boolean).join(" · ");
                    return (
                      // Only the starter opens it: the DM is theirs with the agent.
                      <Pressable key={`${item.ideaId}:${item.starterId}`} disabled={!mine}
                        accessibilityRole={mine ? "button" : undefined} accessibilityLabel={mine ? `Open: ${item.title}` : undefined}
                        onPress={() => item.roomId && onStarted(item.roomId)}
                        style={({ pressed }) => [styles.runningRow, pressed && styles.dim]}>
                        <Text numberOfLines={1} style={[styles.title, { color: palette.fg }]}>{item.title}</Text>
                        <Text numberOfLines={1} style={[styles.why, { color: palette.fgMute }]}>{meta}</Text>
                      </Pressable>
                    );
                  })}
                </View>
              ) : null}
              {shown.map((idea, index) => (
                <Pressable key={idea.id} accessibilityRole="button" accessibilityLabel={idea.title} accessibilityHint={idea.why}
                  onPress={() => setOpenId(idea.id)}
                  style={({ pressed }) => [styles.row, index > 0 && { borderTopColor: palette.edge, borderTopWidth: StyleSheet.hairlineWidth }, pressed && styles.dim]}>
                  <Avatar name={idea.agentName} kind="agent" size={28} palette={palette} />
                  <View style={styles.rowText}>
                    <Text numberOfLines={2} style={[styles.title, { color: palette.fg }]}>{idea.title}</Text>
                    <Text numberOfLines={3} style={[styles.why, { color: palette.fgMute }]}>{idea.why}</Text>
                  </View>
                </Pressable>
              ))}
              {!loading && !error && shown.length === 0 ? (
                <Text style={[styles.body, { color: palette.fgMute }]}>No ideas right now.</Text>
              ) : null}
            </ScrollView>
          </>
        )}
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  sheet: { flex: 1, gap: spacing.md, padding: spacing.lg, paddingBottom: 0 },
  heading: { fontFamily: fonts.sansMedium, fontSize: 22, lineHeight: 28 },
  row: { flexDirection: "row", gap: spacing.md, paddingVertical: spacing.md },
  rowText: { flex: 1, gap: spacing.xs },
  title: { fontFamily: fonts.sansMedium, fontSize: 16, lineHeight: 22 },
  body: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 21 },
  why: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  detail: { gap: spacing.md, paddingBottom: spacing.xl },
  back: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
  detailTitle: { fontFamily: fonts.sansMedium, fontSize: 20, lineHeight: 26 },
  included: { gap: spacing.sm, paddingTop: spacing.md, borderTopWidth: StyleSheet.hairlineWidth },
  check: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  checkText: { flex: 1 },
  actions: { flexDirection: "row", gap: spacing.sm },
  more: { width: 44, alignItems: "center", justifyContent: "center", borderRadius: radii.md, borderWidth: 1 },
  go: { flex: 1, alignItems: "center", borderRadius: radii.md, paddingVertical: spacing.md },
  goText: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  dim: { opacity: 0.6 },
  running: { gap: spacing.sm, paddingBottom: spacing.md },
  runningRow: { gap: 2 },
  section: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
});
