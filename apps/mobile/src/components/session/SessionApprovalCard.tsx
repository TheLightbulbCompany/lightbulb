import {
  APPROVAL_MODE_LABEL, useSessionApproval,
  type LocalSession, type PrivateSendApproval, type SessionControlClient,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

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
  const [detail, setDetail] = useState<PrivateSendApproval | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const privateSend = pending?.tool === "mcp__lightbulb_native_sessions__send";
  const approvalId = pending?.approvalId;
  const inputDigest = pending?.inputDigest;
  const detailKey = `${approvalId}:${inputDigest}`;
  useEffect(() => {
    if (!own || !privateSend || !approvalId || !inputDigest || !client.getPrivateSendApproval) return;
    let active = true;
    void client.getPrivateSendApproval(session.sessionId)
      .then((value) => { if (active) { setDetail(value); setFailedFor(null); } })
      .catch(() => { if (active) setFailedFor(detailKey); });
    return () => { active = false; };
  }, [client, own, privateSend, approvalId, inputDigest, detailKey, retry, session.sessionId]);
  if (session.provider !== "claude" || session.status !== "waiting-approval" || !pending) return null;

  const busy = state?.status === "sending" || state?.status === "sent";
  const denying = denyingFor === pending.approvalId;
  const shown = own && privateSend && detail?.approvalId === pending.approvalId && detail.inputDigest === pending.inputDigest ? detail : null;
  const canApprove = privateSend ? !!shown : pending.exact;
  const canAnswer = allowed && (!privateSend || own);
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
      {shown && <View style={[styles.summary, { backgroundColor: palette.raised }]}>
        <Text selectable style={[styles.privateLine, { color: palette.fg }]}>Destination: {shown.destination}</Text>
        <Text selectable style={[styles.privateLine, { color: palette.fg }]}>Request ID: {shown.requestId}</Text>
        <ScrollView style={styles.privateBody} accessibilityLabel="Message to send">
          <Text selectable style={[styles.privateLine, { color: palette.fg }]}>{shown.text}</Text>
        </ScrollView>
      </View>}
      {!canAnswer ? (
        <Text style={[styles.note, { color: palette.fgSoft }]}>Waiting on {where} approval.</Text>
      ) : (
        <>
          {!canApprove && (
            <Text style={[styles.note, { color: palette.fgSoft }]}>
              {privateSend ? failedFor === detailKey
                ? "Could not load the exact message. Retry, or approve on your Mac."
                : "Loading the exact message…"
                : `Approve this in the terminal on ${where} Mac: Lightbulb cannot show all of it. You can deny it here.`}
            </Text>
          )}
          {privateSend && !shown && failedFor === detailKey && <ApprovalButton label="Retry loading message" onPress={() => { setFailedFor(null); setRetry(value => value + 1); }} disabled={busy} />}
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
              {canApprove && <ApprovalButton label="Approve" onPress={() => void answer({ decision: "allow" })} disabled={busy} primary />}
              {canApprove && pending.modes.map((mode) => (
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
  privateBody: { maxHeight: 220 },
  privateLine: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17 },
  note: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 17 },
  input: { borderWidth: 1, borderRadius: radii.md, minHeight: 44, paddingHorizontal: spacing.sm, fontFamily: fonts.sans, fontSize: 14 },
  actions: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm },
  button: { minHeight: 44, paddingHorizontal: spacing.md, borderWidth: 1, borderRadius: radii.md, justifyContent: "center" },
  buttonText: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  disabled: { opacity: 0.5 },
});
