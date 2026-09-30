import { useAuth } from "@clerk/clerk-expo";
import { createApiClient, VoiceCallSession, type VoiceCallView } from "@lightbulb/shared";
import { router, Stack, useLocalSearchParams } from "expo-router";
import { SymbolView } from "expo-symbols";
import { useEffect, useMemo, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { radii, spacing, useChatPalette } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { nativeVoiceRtc } from "../../../lib/voiceRtc";
import { useLiveEvents } from "../../../providers/live-events";

const ERRORS: Record<string, string> = {
  free_minutes_used: "You've used today's free call minutes.",
  credits_low: "Add credits to keep calling.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
  connection_lost: "The call dropped.",
  voice_busy: "Someone in your workspace is already on a free call.",
};

export default function CallScreen() {
  const { agentId, name } = useLocalSearchParams<{ agentId: string; name?: string }>();
  const agentName = name || "Agent";
  const palette = useChatPalette();
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const { onEvent } = useLiveEvents();
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const [view, setView] = useState<VoiceCallView | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const session = useRef<VoiceCallSession | null>(null);

  useEffect(() => {
    const s = new VoiceCallSession({ api, rtc: nativeVoiceRtc, onEvent: (h) => onEventRef.current(h), agentName });
    session.current = s;
    s.subscribe(setView);
    void s.start(agentId);
    return () => void s.hangUp();
  }, [api, agentId, agentName]);

  useEffect(() => {
    if (view?.state !== "live") return;
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [view?.state]);

  // Once the call is over (hung up, dropped) the modal has nothing left to show.
  useEffect(() => {
    if (view?.state === "ended") router.back();
  }, [view?.state]);

  const left = view?.deadline ? Math.max(0, Math.round((view.deadline - now) / 1000)) : null;
  const failed = view?.state === "failed";
  const status =
    !view || view.state === "connecting" ? "Connecting…"
    : view.state === "live" ? (view.working ? `${agentName} is working: ${view.working}` : "On the call")
    : view.state === "ending" ? "Ending…"
    : failed ? (ERRORS[view.error ?? ""] ?? "The call couldn't start.")
    : "";

  return (
    <View style={[styles.container, { backgroundColor: palette.ground }]}>
      <Stack.Screen options={{ presentation: "modal", title: agentName }} />
      <Text style={[styles.name, { color: palette.fg }]}>{agentName}</Text>
      <Text style={[styles.status, { color: failed ? palette.alert : palette.fgSoft }]} accessibilityLiveRegion="polite">{status}</Text>
      {left !== null && view?.state === "live" && (
        <Text style={[styles.left, { color: palette.fgMute }]}>{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</Text>
      )}
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={failed ? "Close" : "Hang up"}
        onPress={() => { if (failed) router.back(); else void session.current?.hangUp().then(() => router.back()); }}
        style={[styles.button, { backgroundColor: failed ? palette.chip : palette.alert }]}
      >
        {failed
          ? <Text style={{ color: palette.fg }}>Close</Text>
          : <SymbolView accessibilityElementsHidden name="phone.down.fill" size={24} tintColor={palette.onFg} />}
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, alignItems: "center", justifyContent: "center", gap: spacing.md },
  name: { fontSize: 28, fontWeight: "600" },
  status: { fontSize: 16, textAlign: "center", paddingHorizontal: spacing.lg },
  left: { fontSize: 14 },
  button: { marginTop: spacing.lg, minWidth: 64, height: 64, borderRadius: radii.lg, alignItems: "center", justifyContent: "center", paddingHorizontal: spacing.lg },
});
