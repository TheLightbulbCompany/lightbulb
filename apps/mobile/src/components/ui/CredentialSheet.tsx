import { useEffect, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { BottomSheet } from "./BottomSheet";

export type CredentialField = "site" | "username" | "password";
export interface CredentialValues {
  site: string;
  username: string;
  password: string;
}

const LABELS: Record<CredentialField, string> = {
  site: "Website address",
  username: "Username or email",
  password: "Password",
};

/**
 * The sheet a website sign-in is typed into: any of site, username and
 * password. Every shown field is required. The password is secure entry and
 * leaves this component's state on submit and on close; it is never shown back.
 */
export function CredentialSheet({
  visible,
  title,
  hint,
  fields,
  submitLabel,
  initialUsername = "",
  onSubmit,
  onClose,
}: {
  visible: boolean;
  title: string;
  hint?: string | null;
  fields: readonly CredentialField[];
  submitLabel: string;
  initialUsername?: string;
  onSubmit: (values: CredentialValues) => void;
  onClose: () => void;
}) {
  const p = useChatPalette();
  const [values, setValues] = useState<CredentialValues>({ site: "", username: initialUsername, password: "" });
  // One submit per open: the sheet stays mounted through its close animation.
  const submittedRef = useRef(false);
  useEffect(() => {
    setValues({ site: "", username: visible ? initialUsername : "", password: "" });
    submittedRef.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- initialUsername is a seed, not a live binding
  }, [visible]);

  const ready = fields.every((field) => (field === "password" ? values.password : values[field].trim()).length > 0);
  const submit = () => {
    if (!ready || submittedRef.current) return;
    submittedRef.current = true;
    onSubmit({ site: values.site.trim(), username: values.username.trim(), password: values.password });
    setValues((current) => ({ ...current, password: "" }));
    onClose();
  };

  return (
    <BottomSheet visible={visible} onClose={onClose}>
      <View style={[styles.card, { backgroundColor: p.surface }]}>
        <Text style={[styles.title, { color: p.fg }]}>{title}</Text>
        {hint ? <Text style={[styles.hint, { color: p.fgSoft }]}>{hint}</Text> : null}
        {fields.map((field, index) => (
          <TextInput
            key={field}
            style={[styles.input, { backgroundColor: p.raised, borderColor: p.edge, color: p.fg }]}
            placeholder={LABELS[field]}
            placeholderTextColor={p.fgMute}
            accessibilityLabel={LABELS[field]}
            value={values[field]}
            onChangeText={(text) => setValues((current) => ({ ...current, [field]: text }))}
            autoFocus={index === 0}
            autoCapitalize="none"
            autoCorrect={false}
            spellCheck={false}
            secureTextEntry={field === "password"}
            keyboardType={field === "site" ? "url" : "default"}
            textContentType={field === "site" ? "URL" : field}
            autoComplete={field === "site" ? "url" : field === "password" ? "current-password" : "username"}
            returnKeyType={index === fields.length - 1 ? "done" : "next"}
            onSubmitEditing={index === fields.length - 1 ? submit : undefined}
            maxLength={field === "password" ? 4096 : field === "site" ? 2048 : 512}
          />
        ))}
        <View style={styles.actions}>
          <Pressable
            style={({ pressed }) => [styles.button, pressed && styles.pressed]}
            onPress={onClose}
            accessibilityRole="button"
            accessibilityLabel="Cancel"
          >
            <Text style={[styles.buttonText, { color: p.fgMute }]}>Cancel</Text>
          </Pressable>
          <Pressable
            style={({ pressed }) => [styles.button, { backgroundColor: p.fg }, !ready && styles.disabled, pressed && styles.pressed]}
            onPress={submit}
            disabled={!ready}
            accessibilityRole="button"
            accessibilityLabel={submitLabel}
          >
            <Text style={[styles.buttonText, { color: p.onFg }]}>{submitLabel}</Text>
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
    padding: spacing.lg,
    paddingBottom: spacing.md,
    gap: spacing.md,
  },
  title: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  hint: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  // minHeight, not height: the field grows with the reader's text size.
  input: {
    minHeight: 44,
    borderWidth: 1,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    fontFamily: fonts.sans,
    fontSize: 15,
  },
  actions: { flexDirection: "row", justifyContent: "flex-end", gap: spacing.sm },
  button: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm, borderRadius: radii.sm },
  buttonText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
  disabled: { opacity: 0.5 },
  pressed: { opacity: 0.6 },
});
