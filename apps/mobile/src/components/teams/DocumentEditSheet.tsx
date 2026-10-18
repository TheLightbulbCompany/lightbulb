import { ApiError } from "@lightbulb/shared";
import { useState } from "react";
import { Alert, Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import type { IssueDocument } from "../../lib/teamsData";
import { BottomSheet } from "../ui/BottomSheet";

/** A stale save: the server answers 409, or names a conflict/lock in the body. */
function isConflict(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  return err.status === 409 || /conflict|locked/i.test(JSON.stringify(err.body ?? ""));
}

/**
 * Plain editor for one document. `onSave` rejects on failure; the sheet then
 * stays open with the draft so nothing typed is lost.
 */
export function DocumentEditSheet({
  document,
  onSave,
  onClose,
}: {
  document: IssueDocument;
  onSave: (title: string, body: string) => Promise<void>;
  onClose: () => void;
}) {
  const p = useChatPalette();
  const [title, setTitle] = useState(document.title ?? document.key);
  const [body, setBody] = useState(document.body ?? "");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await onSave(title.trim() || document.key, body);
      onClose();
    } catch (err) {
      if (isConflict(err)) {
        Alert.alert(
          "Couldn’t save",
          "This document changed since you opened it. Copy your edits, close this, and open it again.",
        );
      } else {
        Alert.alert("Couldn’t save", "The document could not be saved.");
      }
      setBusy(false);
    }
  };

  const input = { color: p.fg, backgroundColor: p.raised, borderColor: p.edge };
  return (
    <BottomSheet visible onClose={() => !busy && onClose()} large dismissible={!busy}>
      <View style={[styles.sheet, { backgroundColor: p.surface }]}>
        <Text style={[styles.heading, { color: p.fg }]}>Edit document</Text>
        <TextInput
          style={[styles.title, input]}
          value={title}
          onChangeText={setTitle}
          accessibilityLabel="Document title"
        />
        <TextInput
          style={[styles.body, input]}
          value={body}
          onChangeText={setBody}
          multiline
          textAlignVertical="top"
          accessibilityLabel="Document body"
        />
        <View style={styles.actions}>
          <Pressable onPress={onClose} disabled={busy} accessibilityRole="button" accessibilityLabel="Cancel">
            <Text style={[styles.action, { color: p.fgMute }]}>Cancel</Text>
          </Pressable>
          <Pressable onPress={save} disabled={busy} accessibilityRole="button" accessibilityLabel="Save">
            <Text style={[styles.action, { color: p.fg }]}>{busy ? "Saving…" : "Save"}</Text>
          </Pressable>
        </View>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  sheet: { flex: 1, padding: spacing.lg, gap: spacing.md },
  heading: { fontFamily: fonts.sansSemiBold, fontSize: 17 },
  title: { borderWidth: StyleSheet.hairlineWidth, borderRadius: radii.md, padding: spacing.md, fontSize: 16 },
  body: { flex: 1, minHeight: 240, borderWidth: StyleSheet.hairlineWidth, borderRadius: radii.md, padding: spacing.md, fontSize: 15 },
  actions: { flexDirection: "row", justifyContent: "space-between" },
  action: { fontFamily: fonts.sansSemiBold, fontSize: 16, padding: spacing.sm },
});
