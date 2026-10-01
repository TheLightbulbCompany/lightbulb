import { type LocalSession } from "@lightbulb/shared";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, spacing, useChatPalette } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

export function SessionRenameSheet({ session, visible, onClose, onSave }: {
  session: LocalSession | null;
  visible: boolean;
  onClose: () => void;
  onSave: (title: string | null) => Promise<unknown>;
}) {
  const palette = useChatPalette();
  const [title, setTitle] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (visible && session) {
      setTitle(session.displayTitle ?? session.title ?? "");
      setError(null);
    }
  }, [visible, session?.sessionId, session?.displayTitle, session?.title]);
  const save = async () => {
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(title.trim() || null);
      onClose();
    } catch {
      setError("Could not rename this session. Try again.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <BottomSheet visible={visible} onClose={onClose}>
      <View style={styles.content}>
        <Text style={[styles.heading, { color: palette.fg }]}>Rename session</Text>
        <Text style={[styles.note, { color: palette.fgSoft }]}>This name appears wherever the session is shared.</Text>
        <TextInput
          value={title}
          onChangeText={setTitle}
          accessibilityLabel="Session name"
          autoFocus
          selectTextOnFocus
          maxLength={120}
          returnKeyType="done"
          onSubmitEditing={() => void save()}
          style={[styles.input, { color: palette.fg, borderColor: palette.edge, backgroundColor: palette.surface }]}
        />
        {error && <Text accessibilityRole="alert" style={[styles.note, { color: palette.alert }]}>{error}</Text>}
        <View style={styles.actions}>
          <Pressable accessibilityRole="button" accessibilityLabel="Cancel rename" onPress={onClose} disabled={saving}>
            <Text style={[styles.action, { color: palette.fgSoft }]}>Cancel</Text>
          </Pressable>
          <Pressable accessibilityRole="button" accessibilityLabel="Save session name" accessibilityState={{ disabled: saving }} onPress={() => void save()} disabled={saving}>
            <Text style={[styles.action, { color: palette.link }]}>{saving ? "Saving…" : "Save"}</Text>
          </Pressable>
        </View>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  content: { paddingHorizontal: spacing.lg, paddingBottom: spacing.lg, gap: spacing.sm },
  heading: { fontFamily: fonts.sansSemiBold, fontSize: 18 },
  note: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  input: { borderWidth: 1, borderRadius: 10, minHeight: 44, paddingHorizontal: spacing.md, fontFamily: fonts.sans, fontSize: 16 },
  actions: { flexDirection: "row", justifyContent: "flex-end", gap: spacing.lg, paddingVertical: spacing.sm },
  action: { fontFamily: fonts.sansSemiBold, fontSize: 15 },
});
