import Feather from "@expo/vector-icons/Feather";
import { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";

// ---------------------------------------------------------------------------
// Issue-comment composer — the chat composer's look (floating white card on
// cream, forest send button) adapted for posting a comment. Send is latched by
// the local `busy` state (the data layer also ref-latches); a truthy result
// clears the input.
// ---------------------------------------------------------------------------

export interface CommentComposerProps {
  /** Returns true on success (input clears), false on failure (text kept). */
  onSubmit: (body: string) => Promise<boolean>;
}

export function CommentComposer({ onSubmit }: CommentComposerProps) {
  const [value, setValue] = useState("");
  const [focused, setFocused] = useState(false);
  const [busy, setBusy] = useState(false);
  const [height, setHeight] = useState(24);

  const canSend = value.trim().length > 0 && !busy;

  const send = async () => {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    const ok = await onSubmit(trimmed);
    if (ok) {
      setValue("");
      setHeight(24);
    }
    setBusy(false);
  };

  return (
    <View style={styles.outer}>
      <View style={[styles.card, focused && styles.cardFocused]}>
        <View style={styles.inputZone}>
          <TextInput
            style={[styles.input, { height }]}
            placeholder="Add a comment"
            placeholderTextColor={colors.placeholder}
            value={value}
            onChangeText={setValue}
            multiline
            editable={!busy}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onContentSizeChange={(e) =>
              setHeight(Math.min(140, Math.max(24, e.nativeEvent.contentSize.height)))
            }
          />
        </View>
        <View style={styles.rail}>
          <Text style={styles.hint}>Comment</Text>
          <Pressable
            style={[styles.sendButton, !canSend && styles.sendDisabled]}
            onPress={send}
            disabled={!canSend}
            accessibilityRole="button"
            accessibilityLabel="Post comment"
          >
            {busy ? (
              <ActivityIndicator size="small" color={colors.white} />
            ) : (
              <Feather
                name="arrow-up"
                size={16}
                color={canSend ? colors.white : colors.placeholder}
              />
            )}
          </Pressable>
        </View>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  outer: {
    paddingHorizontal: spacing.lg,
    paddingBottom: spacing.lg,
    paddingTop: spacing.sm,
    backgroundColor: colors.cream,
    borderTopWidth: 1,
    borderTopColor: colors.edgeSoft,
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
    fontFamily: fonts.dmSans,
    fontSize: 15,
    lineHeight: 24,
    color: colors.ink,
    paddingTop: 0,
    paddingBottom: 0,
    textAlignVertical: "top",
  },
  rail: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingBottom: spacing.sm,
    paddingTop: 6,
  },
  hint: {
    fontFamily: fonts.mono,
    fontSize: 10,
    letterSpacing: 1.2,
    color: colors.muteSoft,
    textTransform: "uppercase",
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
  sendDisabled: { backgroundColor: colors.creamPressed },
});
