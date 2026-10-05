import { useSessionJoinRequest, type LocalSession, type SessionControlClient } from "@lightbulb/shared";
import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { fonts, radii, spacing, useChatPalette } from "../../config/theme";

type Decide = (sessionId: string, userId: string, decision: "allow" | "decline", requestId?: string) => Promise<unknown>;
type JoinDecider = { decideLocalSessionJoin?: Decide; decideLocalSessionPickup?: Decide };

/**
 * What a teammate sees under a session they cannot send to: who can, and a way
 * to ask. The ask reads "Requested" until the owner answers.
 */
export function AskToJoin({ session, client, ownerName }: {
  session: LocalSession;
  client: SessionControlClient;
  ownerName: string;
}) {
  const palette = useChatPalette();
  const insets = useSafeAreaInsets();
  const join = useSessionJoinRequest(client, session);
  // The phone updates on merge and the backend on promotion, so this can run
  // against a backend with no join routes yet. One that has them always
  // sends `allowedSenders`; without it the button would only ever fail.
  const canAsk = join.supported && session.allowedSenders !== undefined;
  const waiting = join.state === "pending";
  const disabled = join.saving || waiting;
  return (
    <View style={[styles.ask, { paddingBottom: Math.max(insets.bottom, spacing.md) }]}>
      <Text style={[styles.line, { color: palette.fgMute }]}>
        {join.state === "declined" ? `${ownerName} declined your request.` : `Only ${ownerName} can send to this session.`}
      </Text>
      {canAsk && (
        <Pressable
          onPress={() => void join.ask()}
          disabled={disabled}
          accessibilityRole="button"
          accessibilityState={{ disabled }}
          style={[styles.button, { borderColor: palette.edge, backgroundColor: palette.surface }, disabled && styles.dimmed]}
        >
          <Text style={[styles.buttonText, { color: palette.fg }]}>{waiting ? "Requested" : "Ask to join"}</Text>
        </Pressable>
      )}
      {join.error && <Text accessibilityRole="alert" style={[styles.line, { color: palette.alert }]}>{join.error}</Text>}
    </View>
  );
}

/**
 * The owner answers "Ask to join" and "Ask to pick up" in the session itself,
 * where the request's notification lands.
 */
export function JoinRequests({ session, client, personName, onAnswered }: {
  session: LocalSession;
  client: JoinDecider;
  personName: (userId: string) => string;
  onAnswered: () => void;
}) {
  return (
    <>
      <Requests
        sessionId={session.sessionId}
        waiting={session.pendingJoins}
        ids={session.pendingJoinIds}
        line={(name) => `${name} wants to join this session.`}
        decide={client.decideLocalSessionJoin}
        personName={personName}
        onAnswered={onAnswered}
      />
      <Requests
        sessionId={session.sessionId}
        waiting={session.pendingPickups}
        ids={session.pendingPickupIds}
        line={(name) => `${name} wants to pick up this session.`}
        decide={client.decideLocalSessionPickup}
        personName={personName}
        onAnswered={onAnswered}
      />
    </>
  );
}

/** One answered here leaves at once; the session's own refresh then confirms it. */
function Requests({ sessionId, waiting: asked, ids, line, decide: send, personName, onAnswered }: {
  sessionId: string;
  waiting?: string[];
  ids?: Record<string, string>;
  line: (name: string) => string;
  decide?: Decide;
  personName: (userId: string) => string;
  onAnswered: () => void;
}) {
  const palette = useChatPalette();
  // Keyed by request, not person: someone who asks again after a decline is a new request.
  const key = (who: string) => `${who}:${ids?.[who] ?? ""}`;
  const [busy, setBusy] = useState<string[]>([]);
  const [answered, setAnswered] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const waiting = (asked ?? []).filter((who) => !answered.includes(key(who)));
  if (!send || waiting.length === 0) return null;
  const decide = async (who: string, decision: "allow" | "decline") => {
    const id = key(who);
    setBusy((list) => [...list, id]);
    setError(null);
    try {
      // The request the owner saw, so a stale tap never answers a newer one.
      await send(sessionId, who, decision, ids?.[who]);
      setAnswered((list) => [...list, id]);
      onAnswered();
    } catch {
      setError("Could not answer that request. Try again.");
    } finally {
      setBusy((list) => list.filter((x) => x !== id));
    }
  };
  return (
    <View style={styles.requests} accessibilityLiveRegion="polite">
      {waiting.map((who) => (
        <View key={who} style={[styles.request, { backgroundColor: palette.surface, borderColor: palette.edge }]}>
          <Text style={[styles.requestText, { color: palette.fg }]}>{line(personName(who))}</Text>
          <View style={styles.answers}>
            <Pressable
              onPress={() => void decide(who, "allow")}
              disabled={busy.includes(key(who))}
              accessibilityRole="button"
              accessibilityLabel={`Allow ${personName(who)}`}
              style={[styles.button, { borderColor: palette.fg, backgroundColor: palette.fg }, busy.includes(key(who)) && styles.dimmed]}
            >
              <Text style={[styles.buttonText, { color: palette.onFg }]}>Allow</Text>
            </Pressable>
            <Pressable
              onPress={() => void decide(who, "decline")}
              disabled={busy.includes(key(who))}
              accessibilityRole="button"
              accessibilityLabel={`Decline ${personName(who)}`}
              style={[styles.button, { borderColor: palette.edge, backgroundColor: palette.surface }, busy.includes(key(who)) && styles.dimmed]}
            >
              <Text style={[styles.buttonText, { color: palette.fg }]}>Decline</Text>
            </Pressable>
          </View>
        </View>
      ))}
      {error && <Text accessibilityRole="alert" style={[styles.line, { color: palette.alert }]}>{error}</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  ask: { paddingHorizontal: spacing.lg, paddingTop: spacing.md, alignItems: "center", gap: spacing.sm },
  line: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, textAlign: "center" },
  // 44pt tall: the smallest target a thumb hits reliably.
  button: {
    minHeight: 44,
    paddingHorizontal: spacing.lg,
    borderWidth: 1,
    borderRadius: radii.lg,
    alignItems: "center",
    justifyContent: "center",
  },
  buttonText: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  dimmed: { opacity: 0.5 },
  requests: { marginHorizontal: spacing.lg, marginTop: spacing.sm, gap: spacing.sm },
  request: { padding: spacing.md, borderWidth: 1, borderRadius: radii.lg, gap: spacing.sm },
  requestText: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18 },
  answers: { flexDirection: "row", gap: spacing.sm },
});
