import {
  APPROVAL_MODE_LABEL, useSessionApproval,
  type LocalSession, type SessionControlClient,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";

/** The Mac holds an exact Claude permission prompt while a person answers here. */
export function SessionApprovalCard({ session, client, allowed, own, ownerName }: {
  session: LocalSession;
  client: SessionControlClient;
  allowed: boolean;
  own: boolean;
  ownerName: string;
}) {
  const palette = useChatPalette();
  const { pending, answer, state } = useSessionApproval(client, session, randomUUID);
  const [denyingFor, setDenyingFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  if (session.provider !== "claude" || session.status !== "waiting-approval" || !pending) return null;

  const busy = state?.status === "sending" || state?.status === "sent";
  const denying = denyingFor === pending.approvalId;
  const where = own ? "your" : `${ownerName}’s`;
  const deny = () => {
    void answer({ decision: "deny", reason });
  };
  return (
    <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.alert }]} accessibilityLabel="Claude permission prompt">
      <Text style={[styles.title, { color: palette.fg }]}>Claude wants to use {pending.tool}</Text>
      {pending.summary ? (
        <Text selectable style={[styles.summary, { backgroundColor: palette.raised, color: palette.fg }]}>{pending.summary}</Text>
      ) : null}
      {!allowed ? (
        <Text style={[styles.note, { color: palette.fgSoft }]}>Waiting on {where} approval.</Text>
      ) : (
        <>
          {!pending.exact && (
            <Text style={[styles.note, { color: palette.fgSoft }]}>
              Approve this in the terminal on {where} Mac: Lightbulb cannot show all of it. You can deny it here.
            </Text>
          )}
          {denying ? (
            <>
              <TextInput
                value={reason}
                onChangeText={setReason}
                maxLength={500}
                placeholder="Reason (optional)"
                placeholderTextColor={palette.fgMute}
                accessibilityLabel="Reason for denial"
                style={[styles.input, { borderColor: palette.edge, color: palette.fg }]}
              />
              <View style={styles.actions}>
                <ApprovalButton label="Deny" onPress={deny} disabled={busy} destructive />
                <ApprovalButton label="Back" onPress={() => setDenyingFor(null)} disabled={busy} />
              </View>
            </>
          ) : (
            <View style={styles.actions}>
              {pending.exact && <ApprovalButton label="Approve" onPress={() => void answer({ decision: "allow" })} disabled={busy} primary />}
              {pending.exact && pending.modes.map((mode) => (
                <ApprovalButton key={mode} label={`Approve and switch to ${APPROVAL_MODE_LABEL[mode]}`} onPress={() => void answer({ decision: "allow", mode })} disabled={busy} />
              ))}
              <ApprovalButton label="Deny…" onPress={() => setDenyingFor(pending.approvalId)} disabled={busy} />
            </View>
          )}
          {state?.status === "sending" && <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>Sending your answer…</Text>}
          {state?.status === "sent" && <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>Sent to {where} Mac.</Text>}
          {state?.error && <Text accessibilityRole="alert" style={[styles.note, { color: palette.alert }]}>{state.error}</Text>}
        </>
      )}
    </View>
  );
}

function ApprovalButton({ label, onPress, disabled, primary = false, destructive = false }: {
  label: string;
  onPress: () => void;
  disabled: boolean;
  primary?: boolean;
  destructive?: boolean;
}) {
  const palette = useChatPalette();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={[styles.button, { backgroundColor: primary ? palette.fg : palette.surface, borderColor: destructive ? palette.alert : palette.edge }, disabled && styles.disabled]}
    >
      <Text style={[styles.buttonText, { color: primary ? palette.onFg : destructive ? palette.alert : palette.fg }]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: { marginHorizontal: spacing.lg, marginTop: spacing.sm, padding: spacing.md, borderWidth: 1, borderRadius: radii.lg, gap: spacing.sm },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  summary: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17, padding: spacing.sm, borderRadius: radii.md },
  note: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 17 },
  input: { borderWidth: 1, borderRadius: radii.md, minHeight: 44, paddingHorizontal: spacing.sm, fontFamily: fonts.sans, fontSize: 14 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm },
  button: { minHeight: 44, paddingHorizontal: spacing.md, borderWidth: 1, borderRadius: radii.md, justifyContent: "center" },
  buttonText: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  disabled: { opacity: 0.5 },
});
