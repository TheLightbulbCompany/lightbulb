import {
  APPROVALS_LABEL,
  answerHold,
  approvalsLinked,
  holdAnswers,
  holdText,
  listHolds,
  setApprovalsLinked,
  type ApiMethods,
  type HoldAnswer,
  type NetworkHold,
} from "@lightbulb/shared";
import { disabled } from "@expo/ui/swift-ui/modifiers";
import { useEffect, useRef, useState } from "react";
import { AccessibilityInfo, Pressable, StyleSheet, Text, View } from "react-native";

import { Section, Text as UIText, Toggle } from "../ui/swiftui";
import { fonts, spacing, useChatPalette } from "../../config/theme";
import { useDesktopSessionApi } from "../../lib/desktopSessions";

type Api = Pick<ApiMethods, "get" | "post" | "del">;

/** The phone's stable Clerk-authenticated client (the same one sessions use). */
export const useNetworkApi: () => Api = useDesktopSessionApi;

/**
 * This person's open Lightbulb Connect holds, read while the screen is visible. A new
 * `reloadKey` (a hold push tapped while the screen is already up) reads them again at once.
 */
export function useNetworkHolds(api: Api, visible: boolean, reloadKey?: string): NetworkHold[] {
  const [holds, setHolds] = useState<NetworkHold[]>([]);
  useEffect(() => {
    if (!visible) return;
    let live = true;
    const load = () => void listHolds(api).then((next) => { if (live) setHolds(next); }).catch(() => {});
    load();
    const timer = setInterval(load, 30_000);
    return () => { live = false; clearInterval(timer); };
  }, [api, visible, reloadKey]);
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
      .then((line) => {
        setResult(line);
        AccessibilityInfo.announceForAccessibility(line);
      })
      .catch(() => setFailure("That didn’t go through. Try again."))
      .finally(() => setBusy(false));
  };
  return (
    <View style={[styles.card, { backgroundColor: highlighted ? palette.selected : palette.raised, borderColor: palette.edge }]}>
      <Text style={[styles.text, { color: palette.fg }]}>{holdText(hold)}</Text>
      {result ? (
        <Text accessibilityLiveRegion="polite" style={[styles.result, { color: palette.fgMute }]}>{result}</Text>
      ) : (
        <View style={styles.answers}>
          {holdAnswers(hold).map((choice, index) => (
            <Pressable
              key={choice.id}
              disabled={busy}
              hitSlop={6}
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

/**
 * Settings: route this person's holds to the phone and the web (on writes the link, off
 * removes it), as its own section. One write at a time; a failed write puts the toggle
 * back (remounted, since SwiftUI's Toggle keeps its own state) and says so.
 */
export function ApprovalsToggle({ api }: { api: Api }) {
  const [linked, setLinked] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  const inFlight = useRef(false);
  useEffect(() => {
    let live = true;
    // No connected workspace (or no answer): no section.
    approvalsLinked(api).then((value) => { if (live) setLinked(value); }).catch(() => {});
    return () => { live = false; };
  }, [api]);
  if (linked === null) return null;
  const change = (next: boolean) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setApprovalsLinked(api, next)
      .then(() => setLinked(next))
      .catch(() => {
        setError("Couldn’t save this setting. Try again.");
        setEpoch((value) => value + 1);
      })
      .finally(() => {
        inFlight.current = false;
        setBusy(false);
      });
  };
  return (
    <Section footer={error ? <UIText>{error}</UIText> : undefined}>
      <Toggle key={epoch} label={APPROVALS_LABEL} isOn={linked} onIsOnChange={change} modifiers={[disabled(busy)]} />
    </Section>
  );
}

const styles = StyleSheet.create({
  card: { marginHorizontal: spacing.lg, marginTop: spacing.sm, padding: spacing.md, gap: spacing.sm, borderWidth: 1, borderRadius: 12 },
  text: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 21 },
  answers: { flexDirection: "row", flexWrap: "wrap", gap: spacing.xs + 2 },
  pill: { minHeight: 44, justifyContent: "center", paddingHorizontal: spacing.md, borderRadius: 999 },
  pillText: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  result: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
});
