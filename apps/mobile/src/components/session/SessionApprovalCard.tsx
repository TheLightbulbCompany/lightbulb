import {
  APPROVAL_MODE_LABEL, inputDiff, useSessionApproval, useShownInputMatches,
  type LocalSession, type PrivateSendApproval, type SessionControlClient,
} from "@lightbulb/shared";
import { CryptoDigestAlgorithm, digestStringAsync, randomUUID } from "expo-crypto";
import { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";

/** Hex SHA-256, the digest the Mac binds an approval to. */
const sha256Hex = (text: string) => digestStringAsync(CryptoDigestAlgorithm.SHA256, text);

const EDIT_TOOLS = new Set(["Edit", "MultiEdit", "Write"]);

/** The call as the Mac shipped it: a command, an edit's diff, or the input itself. */
function CallInput({ tool, input }: { tool: string; input: Record<string, unknown> }) {
  const palette = useChatPalette();
  const box = [styles.summary, { backgroundColor: palette.raised }];
  if (tool === "Bash" && typeof input.command === "string") {
    return <Text selectable style={[...box, { color: palette.fg }]}>{input.command}</Text>;
  }
  if (EDIT_TOOLS.has(tool)) {
    return (
      <View style={box}>
        {typeof input.file_path === "string" && (
          <Text selectable style={[styles.privateLine, { color: palette.fg }]}>{input.file_path}{input.replace_all === true ? " (every occurrence)" : ""}</Text>
        )}
        <ScrollView style={styles.privateBody} accessibilityLabel="Change">
          {inputDiff(tool, input).map((line, i) => (
            <Text key={i} selectable style={[styles.privateLine, { color: line.mark === "+" ? palette.live : palette.alert }]}>{line.mark + line.text}</Text>
          ))}
        </ScrollView>
      </View>
    );
  }
  return <Text selectable style={[...box, { color: palette.fg }]}>{JSON.stringify(input, null, 2)}</Text>;
}

/** Claude's own permission prompt, held on the Mac and answerable here. */
export function SessionApprovalCard({ session, client, allowed, own, ownerName }: {
  session: LocalSession;
  client: SessionControlClient;
  allowed: boolean;
  own: boolean;
  ownerName: string;
}) {
  const palette = useChatPalette();
  const { pending, answer, state } = useSessionApproval(client, session, randomUUID, sha256Hex);
  const digestOk = useShownInputMatches(session.pendingApproval, sha256Hex);
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
  if (!pending || session.provider !== "claude" || session.status !== "waiting-approval") return null;

  const current = state?.approvalId === pending.approvalId ? state : null;
  const busy = current?.status === "sending" || current?.status === "sent";
  const denying = denyingFor === pending.approvalId;
  const shown = own && privateSend && detail?.approvalId === pending.approvalId && detail.inputDigest === pending.inputDigest ? detail : null;
  const canApprove = privateSend ? !!shown : pending.exact || (pending.full === true && digestOk);
  const canAnswer = allowed && (!privateSend || own);
  const where = own ? "your" : `${ownerName}’s`;
  return (
    <View style={[styles.card, { backgroundColor: palette.surface, borderColor: palette.alert }]} accessibilityLabel="Claude permission prompt">
      <Text style={[styles.title, { color: palette.fg }]}>Claude wants to use {pending.tool}</Text>
      {pending.input && !privateSend ? <CallInput tool={pending.tool} input={pending.input} /> : pending.summary ? (
        <Text selectable style={[styles.summary, { backgroundColor: palette.raised, color: palette.fg }]}>{pending.summary}</Text>
      ) : null}
      {shown && <View style={[styles.summary, { backgroundColor: palette.raised }]}>
        <Text selectable style={[styles.privateLine, { color: palette.fg }]}>To: {shown.destination}</Text>
        <Text selectable style={[styles.privateLine, { color: palette.fgSoft }]}>Request ID: {shown.requestId}</Text>
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
                : `Lightbulb shows only part of this call. Approve it on ${where} Mac; you can deny it here.`}
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
                <ApprovalButton label="Deny" onPress={() => void answer({ decision: "deny", reason })} disabled={busy} destructive />
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
          {current?.status === "sending" && <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>Submitting your decision…</Text>}
          {current?.status === "sent" && <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>Decision queued for {where} Mac. Waiting for the session to apply it.</Text>}
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
