import { useEffect, useState } from "react";
import { TextInput } from "react-native";
import { Host, Button, HStack, Spacer, Text, VStack } from "@expo/ui/swift-ui";
import {
  buttonStyle,
  controlSize,
  disabled,
  font,
  foregroundStyle,
  frame,
  padding,
} from "@expo/ui/swift-ui/modifiers";
import { BottomSheet } from "@/components/ui/BottomSheet";
import { colors } from "@/styles/tokens";

type RoomRenameSheetProps = {
  visible: boolean;
  roomName: string;
  onRename: (name: string) => Promise<void>;
  onClose: () => void;
};

export function RoomRenameSheet({ visible, roomName, onRename, onClose }: RoomRenameSheetProps) {
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

  const save = async () => {
    if (!trimmed) {
      setError("Enter a channel name.");
      return;
    }
    if (trimmed.length > 200) {
      setError("Channel names must be 200 characters or fewer.");
      return;
    }
    if (/\p{C}/u.test(trimmed)) {
      setError("Channel names cannot contain control characters.");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      await onRename(trimmed);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not rename this channel. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <BottomSheet isPresented={visible} onIsPresentedChange={(shown) => !shown && onClose()}>
      <Host matchContents>
        <VStack spacing={16} modifiers={[padding({ all: 16 })]}>
          <HStack alignment="center">
            <Text modifiers={[font({ size: 22, weight: "bold" })]}>Rename channel</Text>
            <Spacer />
            <Button label="Cancel" onPress={onClose} modifiers={[buttonStyle("plain"), foregroundStyle(colors.accent)]} />
          </HStack>
          <TextInput
            accessibilityLabel="Channel name"
            autoFocus
            editable={!saving}
            maxLength={200}
            onChangeText={setName}
            onSubmitEditing={() => void save()}
            placeholder="Channel name"
            returnKeyType="done"
            selectTextOnFocus
            style={{
              backgroundColor: colors.backgroundPrimary,
              borderColor: error ? colors.destructive : colors.separator,
              borderRadius: 10,
              borderWidth: 1,
              color: colors.textPrimary,
              fontSize: 17,
              minHeight: 44,
              paddingHorizontal: 12,
            }}
            value={name}
          />
          {error ? <Text modifiers={[font({ size: 13 }), foregroundStyle(colors.destructive)]}>{error}</Text> : null}
          <Button
            label={saving ? "Saving…" : "Save"}
            onPress={() => void save()}
            modifiers={[
              buttonStyle("borderedProminent"),
              controlSize("large"),
              frame({ maxWidth: Infinity }),
              disabled(!canSave),
            ]}
          />
        </VStack>
      </Host>
    </BottomSheet>
  );
}
