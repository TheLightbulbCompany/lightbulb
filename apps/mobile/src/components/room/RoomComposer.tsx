import Feather from "@expo/vector-icons/Feather";
import { useCallback, useState } from "react";
import { ActivityIndicator, Animated, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { usePulse } from "../chat/usePulse";

function ReconnectingBanner() {
  const pulse = usePulse(true, 0.3, 700);
  return (
    <View style={styles.banner}>
      <Animated.View style={[styles.bannerDot, { opacity: pulse }]} />
      <Text style={styles.bannerText}>Reconnecting live updates. You can keep sending.</Text>
    </View>
  );
}

/**
 * The composer both room screens share: multiline text, a send button, and the
 * reconnecting banner above it.
 *
 * ponytail: no `@`/`:` typeahead and no attach button yet — Task 10 owns both
 * and this file is where they land. It lives here rather than in the
 * conversation screen so the thread screen does not copy 100 lines of card,
 * rail and banner to get an input.
 */
export function RoomComposer({
  placeholder,
  disabled,
  reconnecting,
  onSend,
  onAbort,
}: {
  placeholder: string;
  disabled: boolean;
  reconnecting: boolean;
  onSend: (text: string) => Promise<void>;
  /**
   * Non-null while a run can be stopped. A room can hold several agents, so a
   * run in flight must never block sending — Stop only takes Send's slot while
   * the draft is empty, which is exactly when Send would be disabled anyway.
   */
  onAbort?: (() => void) | null;
}) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const [sending, setSending] = useState(false);
  const body = text.trim();
  const submit = useCallback(async () => {
    if (!body || sending || disabled) return;
    setSending(true);
    // Cleared before the await: a failed send is drawn as its own retry row by
    // the caller, which is where the text survives.
    setText("");
    try {
      await onSend(body);
    } finally {
      setSending(false);
    }
  }, [body, sending, disabled, onSend]);

  return (
    <View style={styles.outer}>
      {reconnecting && <ReconnectingBanner />}
      <View style={[styles.card, focused && styles.cardFocused]}>
        <View style={styles.inputZone}>
          <TextInput
            style={styles.input}
            placeholder={placeholder}
            placeholderTextColor={colors.placeholder}
            value={text}
            onChangeText={setText}
            multiline
            editable={!disabled && !sending}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
          />
        </View>
        <View style={styles.rail}>
          {onAbort && !body ? (
            <Pressable
              style={styles.sendButton}
              onPress={onAbort}
              accessibilityRole="button"
              accessibilityLabel="Stop"
            >
              <Feather name="square" size={14} color={colors.white} />
            </Pressable>
          ) : (
            <Pressable
              style={[styles.sendButton, (!body || disabled) && styles.sendDisabled]}
              onPress={() => void submit()}
              disabled={!body || disabled || sending}
              accessibilityRole="button"
              accessibilityLabel="Send"
            >
              {sending ? (
                <ActivityIndicator size="small" color={colors.white} />
              ) : (
                <Feather name="arrow-up" size={16} color={colors.white} />
              )}
            </Pressable>
          )}
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  outer: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    backgroundColor: colors.cream,
  },
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.xl,
    borderWidth: 1,
    borderColor: colors.edge,
    shadowColor: colors.ink,
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.05,
    shadowRadius: 16,
    elevation: 3,
  },
  cardFocused: {
    borderColor: colors.forestFocusBorder,
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.08,
    shadowRadius: 24,
    elevation: 4,
  },
  inputZone: { paddingHorizontal: spacing.lg, paddingTop: spacing.md },
  input: {
    fontFamily: fonts.sans,
    fontSize: 15,
    lineHeight: 24,
    maxHeight: 200,
    color: colors.ink,
    paddingTop: 0,
    paddingBottom: 0,
    textAlignVertical: "top",
  },
  rail: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    paddingHorizontal: spacing.sm,
    paddingBottom: spacing.sm,
    paddingTop: 6,
  },
  sendButton: {
    marginLeft: "auto",
    width: 32,
    height: 32,
    borderRadius: 16,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  sendDisabled: { opacity: 0.5 },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    backgroundColor: colors.bannerBg,
    borderWidth: 1,
    borderColor: colors.bannerBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
    marginBottom: spacing.sm,
  },
  bannerDot: { width: 6, height: 6, borderRadius: 3, backgroundColor: colors.connectingYellow },
  bannerText: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.bannerText,
  },
});
