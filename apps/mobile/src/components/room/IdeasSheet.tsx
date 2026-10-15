import { useAuth } from "@clerk/expo";
import { ideaSectionKey, ideaSections, orderIdeas, useIdeas, type Idea, type IdeaSectionKey } from "@lightbulb/shared";
import { SymbolView } from "expo-symbols";
import { useState } from "react";
import { ActionSheetIOS, ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, useColorScheme, View } from "react-native";

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

/** A section's tint behind an idea's emoji: the web's people colours (`--person-*`) at 14%. */
const TINTS: Record<"light" | "dark", Record<IdeaSectionKey, string>> = {
  light: { "for-you": "rgba(122,90,0,.14)", money: "rgba(30,107,53,.14)", grow: "rgba(142,31,154,.14)",
    run: "rgba(33,72,184,.14)", build: "rgba(11,99,115,.14)" },
  dark: { "for-you": "rgba(227,195,90,.14)", money: "rgba(127,214,154,.14)", grow: "rgba(227,154,240,.14)",
    run: "rgba(147,180,250,.14)", build: "rgba(103,212,228,.14)" },
};

function IdeaTile({ idea, large = false }: { idea: Idea; large?: boolean }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  return (
    <View accessibilityElementsHidden importantForAccessibility="no-hide-descendants"
      style={[large ? styles.tileLarge : styles.tile, { backgroundColor: TINTS[scheme][ideaSectionKey(idea)] }]}>
      <Text style={large ? styles.tileEmojiLarge : styles.tileEmoji}>{idea.emoji}</Text>
    </View>
  );
}

/**
 * Ideas on the phone, a showroom of what the team can do (owner, 2026-10-15): rows in sections
 * (For you, Money, Grow, Run the day, Build), each with the idea's emoji on its section's tint, a
 * bold outcome and a grey why; a row opens its detail in the sheet (who takes it and how fast,
 * what you get, your part, Let's do it, and "…" → Not interested). A started idea leaves the list
 * (the server hides it while its work is open). No prices (owner ruling and the App Store's).
 */
export function IdeasSheet({ visible, onClose, agentId, onStarted }: IdeasSheetProps) {
  const palette = useChatPalette();
  const { userId, orgId } = useAuth();
  const { messaging } = useLiveEvents();
  const { ideas, loading, error, start, dismiss } = useIdeas(messaging, visible && userId ? `${userId}:${orgId ?? userId}` : null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const shown = orderIdeas(ideas, agentId);
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
            <IdeaTile idea={open} large />
            <Text accessibilityRole="header" style={[styles.detailTitle, { color: palette.fg }]}>{open.title}</Text>
            <Text style={[styles.body, { color: palette.fgSoft }]}>{open.why}</Text>
            <View style={styles.who}>
              <Avatar name={open.agentName} kind="agent" size={20} palette={palette} />
              <Text style={[styles.why, { color: palette.fgSoft }]}>
                {open.agentName} takes this{open.ourTurnaround ? ` · ${open.ourTurnaround}` : ""}
              </Text>
            </View>
            <View style={[styles.included, { borderColor: palette.edge }]}>
              <Text style={[styles.title, { color: palette.fg }]}>What you get</Text>
              {open.deliverables.map((line) => (
                <View key={line} style={styles.check}>
                  <Text style={[styles.body, styles.checkText, { color: palette.fgSoft }]}>{line}</Text>
                  <SymbolView accessibilityElementsHidden name="checkmark.circle" size={18} tintColor={palette.fgMute} />
                </View>
              ))}
            </View>
            {open.yourPart ? <Text style={[styles.why, { color: palette.fgSoft }]}>Your part: {open.yourPart}</Text> : null}
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
              {ideaSections(shown).map((section) => (
                <View key={section.key} style={styles.section}>
                  <Text accessibilityRole="header" style={[styles.sectionTitle, { color: palette.fgMute }]}>{section.title}</Text>
                  {section.ideas.map((idea, index) => (
                    <Pressable key={idea.id} accessibilityRole="button" accessibilityLabel={idea.title} accessibilityHint={idea.why}
                      onPress={() => setOpenId(idea.id)}
                      style={({ pressed }) => [styles.row, index > 0 && { borderTopColor: palette.edge, borderTopWidth: StyleSheet.hairlineWidth }, pressed && styles.dim]}>
                      <IdeaTile idea={idea} />
                      <View style={styles.rowText}>
                        <Text numberOfLines={2} style={[styles.title, { color: palette.fg }]}>{idea.title}</Text>
                        <Text numberOfLines={2} style={[styles.why, { color: palette.fgMute }]}>{idea.why}</Text>
                      </View>
                    </Pressable>
                  ))}
                </View>
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
  section: { paddingBottom: spacing.lg },
  sectionTitle: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.md, paddingVertical: spacing.sm + 2 },
  tile: { width: 40, height: 40, borderRadius: 12, alignItems: "center", justifyContent: "center" },
  tileEmoji: { fontSize: 20, lineHeight: 24 },
  tileLarge: { width: 64, height: 64, borderRadius: 16, alignItems: "center", justifyContent: "center" },
  tileEmojiLarge: { fontSize: 32, lineHeight: 38 },
  who: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
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
});
