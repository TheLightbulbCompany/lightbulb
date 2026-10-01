import {
  APPROVAL_MODE_LABEL, useNativeSendApproval, useSessionApproval,
  type LocalSession, type PrivateSendApproval, type SessionControlClient,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";

/** The owner sees the exact private send before a native session may post it. */
export function SessionApprovalCard({ session, client, allowed, own, ownerName }: {
  session: LocalSession;
  client: SessionControlClient;
  allowed: boolean;
  own: boolean;
  ownerName: string;
}) {
  const palette = useChatPalette();
  const { pending, answer, state } = useSessionApproval(client, session, randomUUID);
  const native = useNativeSendApproval(client, session.sessionId);
  const [denyingFor, setDenyingFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [detail, setDetail] = useState<PrivateSendApproval | null>(null);
  const [failedFor, setFailedFor] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const privateSend = pending?.tool === "mcp__lightbulb_native_sessions__send";
  const nativeSend = !!session.nativeSendApproval && privateSend;
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
  if (!pending || session.status === "ended" || (session.provider === "claude" && session.status !== "waiting-approval" && !nativeSend)) return null;
  if (session.provider === "codex" && !nativeSend) return null;

  const last = nativeSend ? native.state : state;
  const current = last?.approvalId === pending.approvalId ? last : null;
  const busy = current?.status === "sending" || current?.status === "sent";
  const denying = denyingFor === pending.approvalId;
  const shown = own && privateSend && detail?.approvalId === pending.approvalId && detail.inputDigest === pending.inputDigest ? detail : null;
  const canApprove = privateSend ? !!shown && (!nativeSend || session.status !== "offline") : pending.exact;
  const canAnswer = allowed && (!privateSend || own);
  const where = own ? "your" : `${ownerName}’s`;
  const respond = (decision: "allow" | "deny" | "cancel") => {
    if (nativeSend) {
      if (shown) void native.answer(shown, decision, reason);
    } else if (decision !== "cancel") {
      void answer({ decision, reason });
    }
  };
  return (
    <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.alert }]} accessibilityLabel={nativeSend ? `${session.provider === "claude" ? "Claude" : "Codex"} send approval` : "Claude permission prompt"}>
      <Text style={[styles.title, { color: palette.fg }]}>{nativeSend ? `${session.provider === "claude" ? "Claude" : "Codex"} wants to send a Lightbulb message` : `Claude wants to use ${pending.tool}`}</Text>
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
              {nativeSend && session.status === "offline" ? "Your Mac is offline. This message cannot be sent; you can deny or cancel it." : privateSend ? failedFor === detailKey
                ? nativeSend ? "Could not load the exact message. Retry when connected." : "Could not load the exact message. Retry, or approve on your Mac."
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
              <ApprovalButton label="Deny" onPress={() => respond("deny")} disabled={busy || (nativeSend && !shown)} destructive />
                <ApprovalButton label="Back" onPress={() => setDenyingFor(null)} disabled={busy} />
              </View>
            </>
          ) : (
            <View style={styles.actions}>
              {canApprove && <ApprovalButton label="Approve" onPress={() => respond("allow")} disabled={busy} primary />}
              {!nativeSend && canApprove && pending.modes.map((mode) => (
                <ApprovalButton key={mode} label={`Approve and switch to ${APPROVAL_MODE_LABEL[mode]}`} onPress={() => void answer({ decision: "allow", mode })} disabled={busy} />
              ))}
              <ApprovalButton label="Deny…" onPress={() => setDenyingFor(pending.approvalId)} disabled={busy} />
              {nativeSend && shown && <ApprovalButton label="Cancel send" onPress={() => respond("cancel")} disabled={busy} />}
            </View>
          )}
          {current?.status === "sending" && <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>Submitting your decision…</Text>}
          {current?.status === "sent" && <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>{nativeSend ? "Decision recorded." : `Sent to ${where} Mac.`}</Text>}
          {current?.error && <Text accessibilityRole="alert" style={[styles.note, { color: palette.alert }]}>{current.error}</Text>}
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
