import { useAuth } from "@clerk/expo";
import { myStartsIn, notePlanDecision, plansPollMs, useIdeas } from "@lightbulb/shared";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { approveApproval, getApproval, requestApprovalRevision, useTeamsConfig, type TeamsApproval } from "../../lib/teamsData";
import { useLiveEvents } from "../../providers/live-events";
import { MessageMarkdown } from "../chat/MessageMarkdown";

const STATUS_LINE: Record<string, string> = {
  revision_requested: "You asked for changes.",
  approved: "Approved",
  rejected: "Not approved",
  cancelled: "Withdrawn",
};

const text = (approval: TeamsApproval, key: string) => {
  const value = approval.payload?.[key];
  return typeof value === "string" ? value : "";
};

/**
 * The approvals of the viewer's own ideas started in this agent DM (the web's DmIdeaPlan), newest
 * first, one card per start: GET /ideas matches each to its start, so none needs a link in a
 * message. Re-reads /ideas on each new agent message here, and polls (bounded) after Start and
 * after Change something.
 */
export function DmIdeaPlan({ roomId, agentSeq = 0 }: {
  roomId: string;
  /** The newest agent message's seq in this DM: a new one may come with a new approval. */
  agentSeq?: number;
}) {
  const { userId, orgId } = useAuth();
  const { messaging } = useLiveEvents();
  const { running, refresh } = useIdeas(messaging, userId ? `${userId}:${orgId ?? userId}` : null,
    (latest) => plansPollMs(myStartsIn(latest, roomId, userId)));
  // The agent asks approval whenever it reaches an outside action, hours later too: each new
  // message from it re-reads /ideas once (the room's live events already bring it; no polling).
  const seenSeq = useRef(agentSeq);
  useEffect(() => {
    if (agentSeq === seenSeq.current) return;
    seenSeq.current = agentSeq;
    void refresh();
  }, [agentSeq, refresh]);
  const plans = myStartsIn({ running }, roomId, userId).flatMap((start) => (start.plan ? [start.plan] : []));
  return (
    <>
      {plans.map((plan) => (
        <PlanCard key={plan.approvalId} approvalId={plan.approvalId} planStatus={plan.status} onDecided={() => { notePlanDecision(plan.approvalId); void refresh(); }} />
      ))}
    </>
  );
}

/** The approval with Approve / Change something while pending; a status line once decided. */
export function PlanCard({ approvalId, planStatus, onDecided }: {
  approvalId: string;
  /** The plan's status on /ideas: a change (decided, revised) reads the approval again. */
  planStatus?: string;
  onDecided?: () => void;
}) {
  const palette = useChatPalette();
  const config = useTeamsConfig();
  const [approval, setApproval] = useState<TeamsApproval | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);

  const load = useCallback(async () => {
    try {
      setApproval(await getApproval(config, approvalId));
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
    }
  }, [config, approvalId]);
  useEffect(() => { void load(); }, [load, planStatus]);

  const run = async (fn: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setNote(null);
      await load();
      onDecided?.();
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : fallback);
    } finally {
      setBusy(false);
    }
  };

  if (!approval) {
    return loadFailed ? (
      <View accessibilityLabel="Approval" style={[styles.card, styles.row, { borderColor: palette.edge }]}>
        <Text accessibilityRole="alert" style={[styles.meta, { color: palette.fgMute }]}>{"Couldn't load the approval ·"}</Text>
        <Pressable accessibilityRole="button" accessibilityLabel="Retry" onPress={() => void load()}>
          <Text style={[styles.meta, styles.retry, { color: palette.fg }]}>Retry</Text>
        </Pressable>
      </View>
    ) : null;
  }
  const trimmed = note?.trim() ?? "";
  const button = (label: string, onPress: () => void, primary: boolean, disabled = busy) => (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        primary ? { backgroundColor: palette.fg } : { borderColor: palette.edge, borderWidth: 1 },
        (pressed || disabled) && styles.dim,
      ]}
    >
      <Text style={[styles.buttonText, { color: primary ? palette.onFg : palette.fg }]}>{label}</Text>
    </Pressable>
  );

  return (
    <View accessibilityLabel="Approval" style={[styles.card, { borderColor: palette.edge }]}>
      <Text style={[styles.title, { color: palette.fg }]}>{text(approval, "title")}</Text>
      <MessageMarkdown text={text(approval, "summary")} palette={palette} />
      {approval.status !== "pending" ? (
        <Text style={[styles.meta, { color: palette.fgMute }]}>{STATUS_LINE[approval.status] ?? approval.status}</Text>
      ) : note === null ? (
        <View style={styles.row}>
          {button("Approve", () => void run(() => approveApproval(config, approvalId), "It could not be approved. Try again."), true)}
          {button("Change something", () => setNote(""), false)}
        </View>
      ) : (
        <View style={styles.change}>
          <TextInput
            accessibilityLabel="What should change?"
            placeholder="What should change?"
            placeholderTextColor={palette.fgMute}
            autoFocus
            multiline
            maxLength={2000}
            editable={!busy}
            value={note}
            onChangeText={setNote}
            style={[styles.input, { borderColor: palette.edge, color: palette.fg }]}
          />
          <View style={styles.row}>
            {button("Send", () => void run(() => requestApprovalRevision(config, approvalId, trimmed),
              "The change request did not go through. Try again."), true, busy || !trimmed)}
            {button("Cancel", () => setNote(null), false)}
          </View>
        </View>
      )}
      {error ? <Text accessibilityRole="alert" style={[styles.meta, { color: palette.alert }]}>{error}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: { borderWidth: 1, borderRadius: radii.md, padding: spacing.md, gap: spacing.sm, marginHorizontal: spacing.lg, marginVertical: spacing.sm },
  title: { fontFamily: fonts.sansMedium, fontSize: 16, lineHeight: 22 },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  row: { flexDirection: "row", gap: spacing.sm },
  change: { gap: spacing.sm },
  input: { borderWidth: 1, borderRadius: radii.sm, minHeight: 72, padding: spacing.sm, fontFamily: fonts.sans, fontSize: 15, lineHeight: 20 },
  button: { borderRadius: radii.sm, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm },
  buttonText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
  dim: { opacity: 0.6 },
  retry: { fontFamily: fonts.sansMedium, textDecorationLine: "underline" },
});
