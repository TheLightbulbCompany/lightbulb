import { SymbolView } from "expo-symbols";
import { sendBlockedReason, type LocalSession, type useSessionCommand } from "@lightbulb/shared";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { fonts, spacing, useChatPalette } from "../../config/theme";
import { ReceiptChipText } from "./TranscriptRow";

/**
 * The follow-up composer under a shared session: the same pill-and-send field
 * the room composer uses, and the same receipt states the web shows — never an
 * implicit resend. The receipt chip normally rides on the transcript's pending
 * row; `pendingShown` false means that row is gone, so a warning chip lands
 * here instead.
 */
export function SessionPromptRow({ session, text, onChangeText, command, pendingShown = false }: {
  session: LocalSession;
  text: string;
  onChangeText: (text: string) => void;
  command: ReturnType<typeof useSessionCommand>;
  pendingShown?: boolean;
}) {
  const palette = useChatPalette();
  const insets = useSafeAreaInsets();
  const target = session.provider === "claude" ? "Claude" : "Codex";
  const reason = sendBlockedReason(session);
  const chip = command.chip;
  const sendDisabled = command.locked || !text.trim();
  return (
    // Same bottom edge as the room composer: clear of the home indicator at rest,
    // and what `usePushedScreenKeyboardOffset` counts on when the keyboard is up.
    <View style={[styles.wrap, { paddingBottom: Math.max(insets.bottom, spacing.sm), backgroundColor: palette.ground }]}>
      {reason ? (
        <Text style={[styles.reason, { color: palette.warn }]}>{reason}</Text>
      ) : (
        <View style={{ gap: spacing.xs }}>
          {session.status === "offline" && <Text style={[styles.reason, { color: palette.fgSoft }]}>Mac offline. Follow-ups wait up to 24 hours for this session to reconnect.</Text>}
          <View style={styles.row}>
            <View style={[styles.pill, { backgroundColor: palette.surface, borderColor: palette.edge }]}>
              <TextInput
                style={[styles.input, { color: palette.fg }]}
                value={text}
                onChangeText={onChangeText}
                multiline
                editable={!command.locked}
                placeholder={`Send a follow-up to ${target}…`}
                placeholderTextColor={palette.fgMute}
                accessibilityLabel={`Send a follow-up to ${target}`}
              />
            </View>
            <Pressable
              onPress={() => command.send(text)}
              disabled={sendDisabled}
              accessibilityRole="button"
              accessibilityLabel="Send follow-up"
              accessibilityState={{ disabled: sendDisabled }}
              style={[styles.send, { backgroundColor: palette.fg }, sendDisabled && styles.sendDisabled]}
            >
              <SymbolView accessibilityElementsHidden name="arrow.up" size={16} tintColor={palette.onFg} />
            </Pressable>
          </View>
        </View>
      )}
      {(command.canSteer || command.canInterrupt) && (
        <View style={styles.actions}>
          {command.canSteer && (
            <Pressable onPress={() => command.steer(text)} disabled={command.locked || !text.trim()} accessibilityRole="button" accessibilityLabel="Steer current turn" accessibilityState={{ disabled: command.locked || !text.trim() }}>
              <Text style={[styles.link, { color: palette.link }]}>Steer now</Text>
            </Pressable>
          )}
          {command.canInterrupt && (
            <Pressable onPress={command.interrupt} disabled={command.locked} accessibilityRole="button" accessibilityLabel="Stop current turn" accessibilityState={{ disabled: command.locked }}>
              <Text style={[styles.link, { color: palette.alert }]}>Stop turn</Text>
            </Pressable>
          )}
        </View>
      )}
      {(command.canSteer || command.canInterrupt) && <Text style={[styles.reason, { color: palette.fgMute }]}>Send waits for the next turn.</Text>}
      {command.canCancel && <Pressable onPress={() => void command.cancel()} disabled={command.checking} accessibilityRole="button" accessibilityLabel="Cancel queued follow-up">
        <Text style={[styles.link, { color: palette.alert }]}>Cancel queued follow-up</Text>
      </Pressable>}
      {command.attempt?.state === "rejected" && !command.attempt.receipt && command.attempt.error && (
        <Text accessibilityRole="alert" style={[styles.error, { color: palette.alert }]}>{command.attempt.error}</Text>
      )}
      {command.attempt?.receipt?.state === "rejected" && command.attempt.request.kind === "send" && !command.attempt.receipt.cancelledAt && (
        <Pressable onPress={() => command.retry()} disabled={command.locked || !command.canSend} accessibilityRole="button" accessibilityLabel="Retry follow-up" accessibilityState={{ disabled: command.locked || !command.canSend }}>
          <Text style={[styles.link, { color: palette.link }]}>Retry</Text>
        </Pressable>
      )}
      {!pendingShown && chip && command.attempt?.receipt && (chip.tone === "warn" || chip.tone === "error" || command.attempt?.request.kind === "interrupt") && (
        <View style={styles.actions}><ReceiptChipText chip={chip} /></View>
      )}
      {(command.canCheck || command.attempt?.state === "uncertain") && (
        <View style={styles.actions}>
          {command.canCheck && (
            <Pressable onPress={() => void command.checkDelivery()} disabled={command.checking} accessibilityRole="button" accessibilityLabel="Check delivery">
              <Text style={[styles.link, { color: palette.link }]}>Check delivery</Text>
            </Pressable>
          )}
          {command.attempt?.state === "uncertain" && (
            <Pressable onPress={() => command.retry()} disabled={!command.canSend || command.checking} accessibilityRole="button" accessibilityLabel="Retry same request">
              <Text style={[styles.link, { color: palette.link }]}>Retry same request</Text>
            </Pressable>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
    gap: spacing.xs,
  },
  row: { flexDirection: "row", alignItems: "flex-end", gap: spacing.sm },
  pill: {
    flex: 1,
    borderWidth: 1,
    // A capsule 40pt tall on one line, the room composer's field.
    borderRadius: 20,
    paddingHorizontal: 14,
    paddingVertical: spacing.sm,
  },
  input: {
    fontFamily: fonts.sans,
    fontSize: 16,
    lineHeight: 22,
    minHeight: 22,
    maxHeight: 110,
    paddingTop: 0,
    paddingBottom: 0,
  },
  send: { width: 36, height: 36, borderRadius: 18, marginBottom: 2, alignItems: "center", justifyContent: "center" },
  sendDisabled: { opacity: 0.4 },
  reason: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  error: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  actions: { flexDirection: "row", gap: spacing.lg },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
});
