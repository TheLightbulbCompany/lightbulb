import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

type RoomRenameSheetProps = {
  visible: boolean;
  roomName: string;
  onRename: (name: string) => Promise<void>;
  onClose: () => void;
};

const MAX_NAME_LENGTH = 200;

export function RoomRenameSheet({ visible, roomName, onRename, onClose }: RoomRenameSheetProps) {
  const palette = useChatPalette();
  const [name, setName] = useState(roomName);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (visible) {
      setName(roomName);
      setError(null);
      setSaving(false);
    }
  }, [roomName, visible]);

  const trimmed = name.trim();
  const canSave = Boolean(trimmed) && trimmed !== roomName.trim() && !saving;

  const close = () => {
    if (!saving) onClose();
  };

  const save = async () => {
    if (!trimmed) {
      setError("Enter a project name.");
      return;
    }
    if (trimmed.length > MAX_NAME_LENGTH) {
      setError(`Project names must be ${MAX_NAME_LENGTH} characters or fewer.`);
      return;
    }
    if (/\p{C}/u.test(trimmed)) {
      setError("Project names cannot contain control characters.");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await onRename(trimmed);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not rename this project. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <BottomSheet visible={visible} onClose={close} dismissible={!saving}>
      <View style={[styles.card, { backgroundColor: palette.surface }]}>
        <Text style={[styles.title, { color: palette.fg }]}>Rename project</Text>
        <TextInput
          accessibilityLabel="Project name"
          autoCapitalize="words"
          autoCorrect={false}
          autoFocus
          editable={!saving}
          maxLength={MAX_NAME_LENGTH}
          onChangeText={(value) => {
            setName(value);
            setError(null);
          }}
          onSubmitEditing={() => void save()}
          placeholder="Project name"
          placeholderTextColor={palette.fgMute}
          returnKeyType="done"
          selectTextOnFocus
          style={[
            styles.input,
            {
              backgroundColor: palette.raised,
              borderColor: error ? palette.alert : palette.edge,
              color: palette.fg,
            },
          ]}
          value={name}
        />
        {error ? <Text style={[styles.error, { color: palette.alert }]}>{error}</Text> : null}
        <View style={styles.actions}>
          <Pressable
            accessibilityLabel="Cancel"
            accessibilityRole="button"
            disabled={saving}
            onPress={close}
            style={({ pressed }) => [styles.button, pressed && styles.pressed]}
          >
            <Text style={[styles.buttonText, { color: palette.fgMute }]}>Cancel</Text>
          </Pressable>
          <Pressable
            accessibilityLabel="Save"
            accessibilityRole="button"
            disabled={!canSave}
            onPress={() => void save()}
            style={({ pressed }) => [
              styles.button,
              styles.saveButton,
              { backgroundColor: palette.fg },
              !canSave && styles.disabled,
              pressed && styles.pressed,
            ]}
          >
            <Text style={[styles.buttonText, { color: palette.onFg }]}>{saving ? "Saving…" : "Save"}</Text>
          </Pressable>
        </View>
      </View>
    </BottomSheet>
  );
}

const styles = StyleSheet.create({
  card: {
    borderTopLeftRadius: radii.xl,
    borderTopRightRadius: radii.xl,
    gap: spacing.md,
    padding: spacing.lg,
    paddingBottom: spacing.md,
  },
  title: {
    fontFamily: fonts.sansMedium,
    fontSize: 18,
    lineHeight: 24,
  },
  input: {
    borderRadius: radii.md,
    borderWidth: 1,
    fontFamily: fonts.sans,
    fontSize: 16,
    lineHeight: 22,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  error: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
  },
  actions: {
    flexDirection: "row",
    gap: spacing.sm,
    justifyContent: "flex-end",
  },
  button: {
    borderRadius: radii.sm,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  saveButton: {
    minWidth: 72,
  },
  buttonText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    textAlign: "center",
  },
  disabled: { opacity: 0.5 },
  pressed: { opacity: 0.6 },
});
