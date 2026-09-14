import Feather from "@expo/vector-icons/Feather";
import { GLYPH, unavailableReason, type LocalSession, type useSessionCommand } from "@lightbulb/shared";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";

/**
 * The follow-up composer under a shared session: one line in the TUI's prompt
 * glyph, the same receipt states the web shows, and never an implicit resend.
 */
export function SessionPromptRow({ session, text, onChangeText, command }: {
  session: LocalSession;
  text: string;
  onChangeText: (text: string) => void;
  command: ReturnType<typeof useSessionCommand>;
}) {
  const target = session.provider === "claude" ? "Claude" : "Codex";
  const reason = command.canSend ? null
    : session.status === "offline" ? "Offline"
      : session.controlUnavailableReason ? unavailableReason[session.controlUnavailableReason]
        : "This session does not accept remote input.";
  const sendDisabled = command.locked || !text.trim();
  return (
    <View style={styles.wrap}>
      {reason ? (
        <Text style={styles.reason}>{reason}</Text>
      ) : (
        <View style={styles.row}>
          <Text style={styles.glyph}>{GLYPH[session.provider].prompt}</Text>
          <TextInput
            style={styles.input}
            value={text}
            onChangeText={onChangeText}
            multiline
            editable={!command.locked}
            placeholder={`Send a follow-up to ${target}…`}
            placeholderTextColor={colors.placeholder}
            accessibilityLabel={`Send a follow-up to ${target}`}
          />
          <Pressable
            onPress={() => command.send(text)}
            disabled={sendDisabled}
            accessibilityRole="button"
            accessibilityLabel="Send follow-up"
            accessibilityState={{ disabled: sendDisabled }}
            style={[styles.send, sendDisabled && styles.sendDisabled]}
          >
            <Feather name="arrow-up" size={18} color={colors.white} />
          </Pressable>
        </View>
      )}
      {command.attempt?.state === "sending" && <Text style={styles.status}>Sending follow-up…</Text>}
      {command.attempt?.error && <Text accessibilityRole="alert" style={styles.error}>{command.attempt.error}</Text>}
      {command.receiptText && <Text style={styles.status}>{command.receiptText}</Text>}
      {command.receipt?.held && <Text style={styles.status}>Admission is not completed work.</Text>}
      {(command.canCheck || command.attempt?.state === "uncertain") && (
        <View style={styles.actions}>
          {command.canCheck && (
            <Pressable onPress={() => void command.checkDelivery()} disabled={command.checking} accessibilityRole="button" accessibilityLabel="Check delivery">
              <Text style={styles.link}>Check delivery</Text>
            </Pressable>
          )}
          {command.attempt?.state === "uncertain" && (
            <Pressable onPress={() => command.retry()} disabled={!command.canSend || command.checking} accessibilityRole="button" accessibilityLabel="Retry same request">
              <Text style={styles.link}>Retry same request</Text>
            </Pressable>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
    backgroundColor: colors.cream,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    gap: spacing.xs,
  },
  row: { flexDirection: "row", alignItems: "flex-end", gap: spacing.sm },
  glyph: { fontFamily: fonts.mono, fontSize: 15, lineHeight: 22, color: colors.inkSoft, paddingBottom: spacing.sm },
  input: {
    flex: 1,
    minHeight: 38,
    maxHeight: 120,
    fontFamily: fonts.mono,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
    backgroundColor: colors.codeBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.md,
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.sm,
  },
  send: { width: 36, height: 36, borderRadius: 18, backgroundColor: colors.forest, alignItems: "center", justifyContent: "center" },
  sendDisabled: { opacity: 0.4 },
  reason: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.warn },
  status: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  error: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.errorText },
  actions: { flexDirection: "row", gap: spacing.lg },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, color: colors.forest },
});
