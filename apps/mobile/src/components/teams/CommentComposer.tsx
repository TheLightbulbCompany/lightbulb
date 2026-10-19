import { SymbolView } from "../ui/SymbolView";
import { useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  useColorScheme,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";
import { useScaledHeight } from "../../lib/textScale";

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
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const insets = useSafeAreaInsets();
  // One line at rest, roughly six before it scrolls — both at the reader's
  // text size, so a large size gets a taller box rather than fewer lines.
  const minHeight = useScaledHeight(24);
  const maxHeight = useScaledHeight(140);
  const [value, setValue] = useState("");
  const [focused, setFocused] = useState(false);
  const [busy, setBusy] = useState(false);
  // State holds what the field MEASURED, not the height we draw: the reader can
  // change their text size while this is on screen, and a height seeded from
  // the old scale would stay until they next typed. Clamping at render instead
  // lets the box follow the scale immediately.
  const [contentHeight, setContentHeight] = useState(0);
  const height = Math.min(maxHeight, Math.max(minHeight, contentHeight));

  const canSend = value.trim().length > 0 && !busy;

  const send = async () => {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    const ok = await onSubmit(trimmed);
    if (ok) {
      setValue("");
      setContentHeight(0);
    }
    setBusy(false);
  };

  return (
    // Clears the home indicator at rest; with the keyboard up the screen's
    // KeyboardAvoidingView tucks `insets.bottom` behind it, leaving `sm`.
    <View style={[styles.outer, { paddingBottom: insets.bottom + spacing.sm }]}>
      <View style={[styles.card, focused && styles.cardFocused]}>
        <View style={styles.inputZone}>
          <TextInput
            style={[styles.input, { height }]}
            placeholder="Add a comment"
            placeholderTextColor={palette.fgMute}
            value={value}
            onChangeText={setValue}
            multiline
            editable={!busy}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onContentSizeChange={(e) => setContentHeight(e.nativeEvent.contentSize.height)}
          />
        </View>
        <View style={styles.rail}>
          {/* A caption on the send rail, not content: it may grow a little,
              but not enough to wrap the rail or push the button off it. */}
          <Text style={styles.hint} maxFontSizeMultiplier={1.4}>
            Comment
          </Text>
          <Pressable
            style={[styles.sendButton, !canSend && styles.sendDisabled]}
            onPress={send}
            disabled={!canSend}
            accessibilityRole="button"
            accessibilityLabel="Post comment"
          >
            {busy ? (
              <ActivityIndicator size="small" color={palette.onFg} />
            ) : (
              <SymbolView
                accessibilityElementsHidden
                name="arrow.up"
                size={16}
                tintColor={palette.onFg}
              />
            )}
          </Pressable>
        </View>
      </View>
    </View>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    outer: {
      paddingHorizontal: spacing.lg,
      paddingTop: spacing.sm,
      backgroundColor: p.ground,
      borderTopWidth: 1,
      borderTopColor: p.edge,
    },
    card: {
      backgroundColor: p.surface,
      borderRadius: radii.xl,
      borderWidth: 1,
      borderColor: p.edge,
      shadowColor: p.fg,
      shadowOffset: { width: 0, height: 4 },
      shadowOpacity: 0.05,
      shadowRadius: 16,
      elevation: 3,
    },
    cardFocused: {
      borderColor: p.fgMute,
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
      color: p.fg,
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
      color: p.fgMute,
      textTransform: "uppercase",
    },
    sendButton: {
      marginLeft: "auto",
      width: 32,
      height: 32,
      borderRadius: 16,
      backgroundColor: p.fg,
      alignItems: "center",
      justifyContent: "center",
    },
    sendDisabled: { opacity: 0.5 },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
