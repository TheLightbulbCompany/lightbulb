import { useAuth } from "@clerk/expo";
import { createApiClient, renamedText, voiceNoteText } from "@lightbulb/shared";
import { router } from "expo-router";
import { useEffect, useMemo, useRef } from "react";
import { Pressable, StyleSheet, Text, View, type StyleProp, type ViewStyle } from "react-native";

import { makeAdapter } from "../../lib/adapter";
import { radii, spacing, useChatPalette } from "../../config/theme";
import { useLive } from "../../providers/live";

const RENAMED_MS = 10_000;
const NOTE_MS = 6_000;

/** The rename / voice-note banner (UI §5): in the sheet, or on the room screen while the sheet is hidden.
 *  A notice, not a message: nothing is posted to the room. Undo only for the person who asked. */
export function NoticeBanner({ style }: { style?: StyleProp<ViewStyle> }) {
  const { notice, localIdentity, dismissNotice, setOpen } = useLive();
  const palette = useChatPalette();
  const { getToken } = useAuth();
  const tokenRef = useRef(getToken);
  tokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => tokenRef.current())), []);
  const shown = notice && (notice.kind === "renamed" || notice.kind === "voice_note_saved") ? notice : null;

  useEffect(() => {
    if (!shown) return;
    // From arrival, not mount: a notice that landed while no banner was mounted must not get a fresh 10 s.
    const t = setTimeout(dismissNotice, Math.max(0, (shown.kind === "renamed" ? RENAMED_MS : NOTE_MS) - (Date.now() - shown.at)));
    return () => clearTimeout(t);
  }, [shown, dismissNotice]);

  if (!shown) return null;
  const text = shown.kind === "renamed"
    ? `${renamedText(shown.from, shown.to)}${shown.collides ? `. Say 'hey ${shown.to}' in Live.` : ""}`
    : voiceNoteText(shown.name, shown.text);
  const action = shown.kind === "renamed"
    ? shown.by === localIdentity && localIdentity
      ? { label: "Undo", run: () => { dismissNotice(); void api.patch(`/teams/agents/${encodeURIComponent(shown.agentId)}`, { name: shown.from }).catch(() => {}); } }
      : null
    : { label: "Edit", run: () => { dismissNotice(); setOpen(false); router.navigate("/(app)/(tabs)/teams"); } };

  return (
    <View style={[styles.banner, { backgroundColor: palette.surface }, style]} accessibilityRole="alert" accessibilityLiveRegion="polite">
      <Text style={[styles.text, { color: palette.fg }]}>{text}</Text>
      {action && (
        <Pressable accessibilityRole="button" onPress={action.run} hitSlop={8}>
          <Text style={[styles.action, { color: palette.link }]}>{action.label}</Text>
        </Pressable>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  banner: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.md, padding: spacing.md, borderRadius: radii.lg },
  text: { flex: 1, fontSize: 15 },
  action: { fontSize: 17 },
});
