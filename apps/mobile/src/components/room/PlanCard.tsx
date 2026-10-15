import { useAuth } from "@clerk/expo";
import { useIdeas, type IdeasResponse } from "@lightbulb/shared";
import { useCallback, useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { approveApproval, getApproval, requestApprovalRevision, useTeamsConfig, type TeamsApproval } from "../../lib/teamsData";
import { useLiveEvents } from "../../providers/live-events";
import { MessageMarkdown } from "../chat/MessageMarkdown";

/** While the viewer's start in this DM has no plan yet: ask again this often, for this long after Start. */
const PLAN_POLL_MS = 10_000;
const PLAN_POLL_FOR_MS = 5 * 60_000;

const STATUS_LINE: Record<string, string> = {
  revision_requested: "You asked for changes. The plan is being revised.",
  approved: "Plan approved",
  rejected: "Plan not approved",
  cancelled: "Plan withdrawn",
};

const text = (approval: TeamsApproval, key: string) => {
  const value = approval.payload?.[key];
  return typeof value === "string" ? value : "";
};

/**
 * The plan of the viewer's own idea started in this agent DM (the web's DmIdeaPlan): GET /ideas
 * matches it to the start, so it needs no link in a message. Polls while the start has no plan,
 * for five minutes from the Start.
 */
export function DmIdeaPlan({ roomId }: { roomId: string }) {
  const { userId, orgId } = useAuth();
  const { messaging } = useLiveEvents();
  const mineIn = useCallback((data: IdeasResponse | undefined) =>
    data?.running.find((r) => r.roomId === roomId && r.starterId === userId), [roomId, userId]);
  const { running, refresh } = useIdeas(messaging, userId ? `${userId}:${orgId ?? userId}` : null, (latest) => {
    const mine = mineIn(latest);
    const started = Date.parse(mine?.createdAt ?? "");
    return mine && !mine.plan && Date.now() - started < PLAN_POLL_FOR_MS ? PLAN_POLL_MS : 0;
  });
  const plan = mineIn({ ideas: [], running })?.plan;
  return plan ? <PlanCard approvalId={plan.approvalId} onDecided={() => void refresh()} /> : null;
}

/** The plan with Approve plan / Change something while pending; a status line once decided. */
export function PlanCard({ approvalId, onDecided }: { approvalId: string; onDecided?: () => void }) {
  const palette = useChatPalette();
  const config = useTeamsConfig();
  const [approval, setApproval] = useState<TeamsApproval | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setApproval(await getApproval(config, approvalId));
    } catch {
      // The card waits for the next read; the plan is also in Activity.
    }
  }, [config, approvalId]);
  useEffect(() => { void load(); }, [load]);

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

  if (!approval) return null;
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
    <View accessibilityLabel="Plan for approval" style={[styles.card, { borderColor: palette.edge }]}>
      <Text style={[styles.title, { color: palette.fg }]}>{text(approval, "title")}</Text>
      <MessageMarkdown text={text(approval, "summary")} />
      {approval.status !== "pending" ? (
        <Text style={[styles.meta, { color: palette.fgMute }]}>{STATUS_LINE[approval.status] ?? approval.status}</Text>
      ) : note === null ? (
        <View style={styles.row}>
          {button("Approve plan", () => void run(() => approveApproval(config, approvalId), "The plan could not be approved. Try again."), true)}
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
});
