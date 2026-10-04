import type { SessionResumeFlow } from "@lightbulb/shared";
import { useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";

/** Under an ended session: "Resume and send", or where a resume someone asked for stands. */
export function SessionResume({ flow, own, ownerName }: { flow: SessionResumeFlow; own: boolean; ownerName: string }) {
  const palette = useChatPalette();
  const insets = useSafeAreaInsets();
  const [text, setText] = useState("");
  if (!flow.available && !flow.status) return null;
  const label = text.trim() ? "Resume and send" : "Resume";
  return (
    <View style={[styles.wrap, { paddingBottom: Math.max(insets.bottom, spacing.md) }]}>
      {flow.status && <Text accessibilityLiveRegion="polite" style={[styles.line, { color: palette.fgMute }]}>{flow.status}</Text>}
      {flow.available && (
        <>
          <TextInput value={text} onChangeText={setText} multiline accessibilityLabel="Message to send when it resumes"
            placeholder="Message to send when it resumes (optional)" placeholderTextColor={palette.fgMute}
            style={[styles.input, { color: palette.fg, borderColor: palette.edge, backgroundColor: palette.surface }]} />
          <Text style={[styles.line, { color: palette.fgMute }]}>
            {own ? "Starts it again on your Mac, in the same folder." : `Starts it again on ${ownerName}’s Mac in plan mode. ${ownerName} sees it there and can end it.`}
          </Text>
          <Pressable onPress={() => void flow.resume(text).then((ok) => { if (ok) setText(""); })} disabled={flow.saving}
            accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ disabled: flow.saving }}
            style={[styles.button, { borderColor: palette.fg, backgroundColor: palette.fg }, flow.saving && styles.dimmed]}>
            <Text style={[styles.buttonText, { color: palette.onFg }]}>{label}</Text>
          </Pressable>
        </>
      )}
      {flow.error && <Text accessibilityRole="alert" style={[styles.line, { color: palette.alert }]}>{flow.error}</Text>}
    </View>
  );
}

/** Where a resume's first message stands once the session runs again. */
export function ResumeDelivery({ flow }: { flow: SessionResumeFlow }) {
  const palette = useChatPalette();
  if (!flow.chip) return null;
  return (
    <Text accessibilityLiveRegion="polite" style={[styles.delivery, { color: flow.chip.tone === "ok" ? palette.fgMute : palette.alert }]}>
      {`First message: ${flow.chip.label}${flow.chip.detail ? ` · ${flow.chip.detail}` : ""}`}
    </Text>
  );
}

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: spacing.lg, paddingTop: spacing.md, gap: spacing.sm },
  line: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, textAlign: "center" },
  input: { minHeight: 44, maxHeight: 120, borderWidth: 1, borderRadius: radii.lg, paddingHorizontal: spacing.md, paddingVertical: spacing.sm, fontFamily: fonts.sans, fontSize: 15 },
  // 44pt tall: the smallest target a thumb hits reliably.
  button: { minHeight: 44, paddingHorizontal: spacing.lg, borderWidth: 1, borderRadius: radii.lg, alignItems: "center", justifyContent: "center" },
  buttonText: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  dimmed: { opacity: 0.5 },
  delivery: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, paddingHorizontal: spacing.lg, paddingTop: spacing.xs },
});
