import { useAuth } from "@clerk/expo";
import {
  APPROVALS_LABEL,
  answerHold,
  approvalsLinked,
  createApiClient,
  holdAnswers,
  holdText,
  listHolds,
  setApprovalsLinked,
  type ApiMethods,
  type HoldAnswer,
  type NetworkHold,
} from "@lightbulb/shared";
import { useEffect, useMemo, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { Toggle } from "../ui/swiftui";
import { fonts, spacing, useChatPalette } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";

type Api = Pick<ApiMethods, "get" | "post" | "del">;

/** A stable API client (Clerk's getToken changes identity every render, so it rides a ref). */
export function useNetworkApi(): Api {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  return useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
}

/** This person's open Lightbulb Connect holds, read while the screen is visible. */
export function useNetworkHolds(api: Api, visible: boolean): NetworkHold[] {
  const [holds, setHolds] = useState<NetworkHold[]>([]);
  useEffect(() => {
    if (!visible) return;
    let live = true;
    const load = () => void listHolds(api).then((next) => { if (live) setHolds(next); }).catch(() => {});
    load();
    const timer = setInterval(load, 30_000);
    return () => { live = false; clearInterval(timer); };
  }, [api, visible]);
  return holds;
}

/**
 * One hold on the phone: who wants to reach which session on which computer, and its
 * answers. The hold carries no message text, so none is shown. A hold push opens it.
 */
export function HoldCard({ hold, api, highlighted }: { hold: NetworkHold; api: Api; highlighted?: boolean }) {
  const palette = useChatPalette();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const answer = (choice: HoldAnswer) => {
    setBusy(true);
    setFailure(null);
    answerHold(api, hold, choice)
      .then(setResult)
      .catch(() => setFailure("That didn’t go through. Try again."))
      .finally(() => setBusy(false));
  };
  return (
    <View style={[styles.card, { backgroundColor: highlighted ? palette.selected : palette.raised, borderColor: palette.edge }]}>
      <Text style={[styles.text, { color: palette.fg }]}>{holdText(hold)}</Text>
      {result ? (
        <Text accessibilityRole="text" style={[styles.result, { color: palette.fgMute }]}>{result}</Text>
      ) : (
        <View style={styles.answers}>
          {holdAnswers(hold).map((choice, index) => (
            <Pressable
              key={choice.id}
              disabled={busy}
              onPress={() => answer(choice)}
              accessibilityRole="button"
              accessibilityLabel={choice.label}
              style={[styles.pill, { backgroundColor: index === 0 ? palette.fg : palette.chip, opacity: busy ? 0.5 : 1 }]}
            >
              <Text style={[styles.pillText, { color: index === 0 ? palette.onFg : palette.fg }]}>{choice.label}</Text>
            </Pressable>
          ))}
        </View>
      )}
      {failure ? <Text accessibilityRole="alert" style={[styles.result, { color: palette.alert }]}>{failure}</Text> : null}
    </View>
  );
}

/** Settings: route this person's holds to the phone and the web (on writes the link, off removes it). */
export function ApprovalsToggle({ api }: { api: Api }) {
  const [linked, setLinked] = useState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    // No connected workspace (or no answer): no toggle.
    approvalsLinked(api).then((value) => { if (live) setLinked(value); }).catch(() => {});
    return () => { live = false; };
  }, [api]);
  if (linked === null) return null;
  return (
    <Toggle
      label={APPROVALS_LABEL}
      isOn={linked}
      onIsOnChange={(next) => void setApprovalsLinked(api, next).then(() => setLinked(next)).catch(() => setLinked(!next))}
    />
  );
}

const styles = StyleSheet.create({
  card: { marginHorizontal: spacing.lg, marginTop: spacing.sm, padding: spacing.md, gap: spacing.sm, borderWidth: 1, borderRadius: 12 },
  text: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 21 },
  answers: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs + 2 },
  pill: { minHeight: 32, justifyContent: "center", paddingHorizontal: spacing.md, borderRadius: 999 },
  pillText: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  result: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
