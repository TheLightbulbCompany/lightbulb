import { useAuth } from "@clerk/clerk-expo";
import { randomUUID } from "expo-crypto";
import { router, Stack, useFocusEffect } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, Text, TextInput, useColorScheme, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { SegmentedControl } from "../../../components/ui/SegmentedControl";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { usePushedScreenKeyboardOffset } from "../../../lib/keyboardOffset";
import {
  sessionOutcomeError,
  sessionStartError,
  useDesktopSessionApi,
  type SessionMac,
  type SessionOptions,
  type SessionTargets,
} from "../../../lib/desktopSessions";
import { useLiveEvents } from "../../../providers/live-events";

const MAX_PROMPT_BYTES = 4096;
const PENDING_MS = 150_000;
type Pending = {
  startId: string;
  phase: "starting" | "opened" | "delayed" | "failed";
  error?: string;
};

const shortPath = (path: string) => path.replace(/^\/Users\/[^/]+(?=\/|$)/, "~");
const byteLength = (text: string) => Array.from(text).reduce((sum, character) => {
  const point = character.codePointAt(0)!;
  return sum + (point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4);
}, 0);

/** Start one of the caller's installed CLIs in a folder the selected Mac already approved. */
export default function NewSessionScreen() {
  const palette = useChatPalette();
  const insets = useSafeAreaInsets();
  const dark = useColorScheme() === "dark";
  const keyboardOffset = usePushedScreenKeyboardOffset();
  const { userId } = useAuth();
  const api = useDesktopSessionApi();
  const live = useLiveEvents();
  const [targets, setTargets] = useState<SessionTargets | null>(null);
  const [targetsError, setTargetsError] = useState<string | null>(null);
  const [deviceId, setDeviceId] = useState("");
  const [optionsRefresh, setOptionsRefresh] = useState(0);
  const [optionsState, setOptionsState] = useState<{ key: string; options?: SessionOptions; error?: string } | null>(null);
  const [tool, setTool] = useState<"claude" | "codex" | "">("");
  const [folder, setFolder] = useState("");
  const [prompt, setPrompt] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const targetRequest = useRef(0);

  const loadTargets = useCallback(async () => {
    const request = ++targetRequest.current;
    setTargetsError(null);
    try {
      const response = await api.get("/desktop-sessions/targets") as SessionTargets;
      if (request === targetRequest.current) setTargets(response);
    } catch (error) {
      if (request === targetRequest.current) setTargetsError(sessionStartError(error));
    }
  }, [api]);
  useFocusEffect(useCallback(() => {
    void loadTargets();
    return () => { targetRequest.current += 1; };
  }, [loadTargets]));

  const mac: SessionMac | undefined = targets?.devices.find((device) => device.deviceId === deviceId)
    ?? targets?.devices[0];
  const macKey = mac ? `${mac.deviceId}:${mac.generation}` : "";
  useEffect(() => {
    if (!mac) return;
    let active = true;
    setOptionsState({ key: macKey });
    void api.get(`/desktop-sessions/targets/${encodeURIComponent(mac.deviceId)}/options?generation=${encodeURIComponent(mac.generation)}`)
      .then((response) => { if (active) setOptionsState({ key: macKey, options: response as SessionOptions }); })
      .catch((error) => { if (active) setOptionsState({ key: macKey, error: sessionStartError(error) }); });
    return () => { active = false; };
  }, [api, macKey, mac?.deviceId, mac?.generation, optionsRefresh]);
  const options = optionsState?.key === macKey ? optionsState.options : undefined;
  const optionsError = optionsState?.key === macKey ? optionsState.error : undefined;
  const macName = options?.name || mac?.name || "your Mac";
  const selected = options?.tools.find((candidate) => candidate.id === tool) ?? options?.tools[0];
  const folderPath = options?.folders.includes(folder) ? folder : options?.folders[0] ?? "";
  const promptTooLong = byteLength(prompt) > MAX_PROMPT_BYTES;
  const unsafeClaudePrompt = selected?.id === "claude" && /^[a-z][a-z-]*$/.test(prompt.trim());
  const busy = !!pending && pending.phase !== "failed";

  useEffect(() => {
    if (!pending || pending.phase === "failed" || pending.phase === "delayed") return;
    const unsubscribe = live.onEvent((event) => {
      if (event.type !== "runtime.operation.changed" || event.payload.operationId !== pending.startId) return;
      setPending((current) => current?.startId === event.payload.operationId
        ? event.payload.state === "failed"
          ? { ...current, phase: "failed", error: sessionOutcomeError(event.payload.error) }
          : event.payload.state === "succeeded" ? { ...current, phase: "opened" } : current
        : current);
    });
    return () => { unsubscribe(); };
  }, [live, pending?.startId]);
  useEffect(() => {
    if (!pending || pending.phase === "failed" || pending.phase === "delayed") return;
    const timer = setTimeout(() => setPending((current) => current?.startId === pending.startId
      ? current.phase === "opened"
        ? { ...current, phase: "delayed" }
        : { ...current, phase: "failed", error: "No answer from your Mac. Check Terminal there, then try again." }
      : current), PENDING_MS);
    return () => clearTimeout(timer);
  }, [pending?.startId, pending?.phase]);

  const blocked = targetsError
    ?? (!targets ? null
      : !targets.enabled ? "Starting sessions is off for this workspace."
      : !mac ? "No Mac is ready. Open the Lightbulb desktop app on your Mac and sign in."
      : optionsError ? `Can't reach ${macName}. ${optionsError}`
      : !options ? null
      : !options.sharing ? `Session sharing is off on ${macName}. Turn it on in the Lightbulb desktop app.`
      : options.tools.length === 0 ? `Install Claude Code or Codex on ${macName} first.`
      : options.folders.length === 0 ? "Approve a folder in the desktop app: Settings → File access."
      : null);
  const ready = !!userId && !!mac && !!options && !!selected && !!folderPath && !blocked;
  const canStart = ready && !busy && !!prompt.trim() && !promptTooLong && !unsafeClaudePrompt;

  async function start() {
    if (!canStart || !mac || !selected) return;
    const startId = randomUUID();
    setPending({ startId, phase: "starting" });
    try {
      await api.post("/desktop-sessions/start", {
        startId, deviceId: mac.deviceId, generation: mac.generation,
        tool: selected.id, folder: folderPath, model: null, mode: null, prompt,
      });
    } catch (error) {
      setPending((current) => current?.startId === startId
        ? { ...current, phase: "failed", error: sessionStartError(error) } : current);
    }
  }

  const choice = (id: string, label: string, selectedChoice: boolean, onPress: () => void) => (
    <Pressable
      key={id}
      onPress={onPress}
      disabled={busy}
      accessibilityRole="radio"
      accessibilityState={{ selected: selectedChoice, disabled: busy }}
      style={{ minHeight: 48, padding: spacing.md, borderRadius: radii.md,
        backgroundColor: selectedChoice ? palette.selected : palette.raised,
        borderWidth: 1, borderColor: selectedChoice ? palette.link : palette.edge, justifyContent: "center" }}
    >
      <Text style={{ color: palette.fg, fontFamily: selectedChoice ? fonts.sansMedium : fonts.sans, fontSize: 14 }}>
        {label}
      </Text>
    </Pressable>
  );

  return (
    <KeyboardAvoidingView style={{ flex: 1, backgroundColor: palette.ground }} behavior="padding" keyboardVerticalOffset={keyboardOffset}>
      <Stack.Screen options={{ title: "New session" }} />
      <ScrollView contentInsetAdjustmentBehavior="automatic" keyboardShouldPersistTaps="handled"
        contentContainerStyle={{ padding: spacing.lg, paddingBottom: insets.bottom + spacing.xl, gap: spacing.lg }}>
        {!targets && !targetsError ? <ActivityIndicator accessibilityLabel="Finding your Macs" /> : null}
        {blocked ? <Text accessibilityRole="alert" style={{ color: palette.alert, fontFamily: fonts.sans, fontSize: 15 }}>{blocked}</Text> : null}
        {(targetsError || (blocked && targets?.enabled)) ? (
          <Pressable accessibilityRole="button" accessibilityLabel="Try again" onPress={() => {
            void loadTargets();
            if (mac) setOptionsRefresh((current) => current + 1);
          }}>
            <Text style={{ color: palette.link, fontFamily: fonts.sansMedium }}>Try again</Text>
          </Pressable>
        ) : null}
        {targets?.enabled && targets.devices.length > 1 ? (
          <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
            <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Mac</Text>
            {targets.devices.map((device, index) => choice(device.deviceId,
              device.deviceId === mac?.deviceId ? macName : device.name === "Your Mac" ? `Your Mac ${index + 1}` : device.name,
              device.deviceId === mac?.deviceId,
              () => { setDeviceId(device.deviceId); setFolder(""); setTool(""); },
            ))}
          </View>
        ) : null}
        {mac && !options && !optionsError ? <ActivityIndicator accessibilityLabel="Checking your Mac" /> : null}
        {options?.sharing && options.tools.length > 0 ? (
          <View style={{ gap: spacing.sm }}>
            <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Tool on {macName}</Text>
            <View pointerEvents={busy ? "none" : "auto"} style={{ opacity: busy ? 0.5 : 1 }}>
              <SegmentedControl label="CLI tool" segments={options.tools.map((item) => ({ key: item.id, label: item.label }))}
                value={selected!.id} onChange={setTool} colorScheme={dark ? "dark" : "light"} />
            </View>
          </View>
        ) : null}
        {options?.sharing && options.folders.length > 0 ? (
          <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
            <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Approved folder</Text>
            {options.folders.map((path) => choice(path, shortPath(path), path === folderPath, () => setFolder(path)))}
          </View>
        ) : null}
        {options?.sharing && selected && folderPath ? (
          <View style={{ gap: spacing.sm }}>
            <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>First message</Text>
            <TextInput
              accessibilityLabel="First message"
              placeholder="What do you want to do?"
              placeholderTextColor={palette.fgMute}
              multiline
              value={prompt}
              onChangeText={setPrompt}
              editable={!busy}
              style={{ minHeight: 104, padding: spacing.md, borderRadius: radii.md,
                borderWidth: 1, borderColor: palette.edge, backgroundColor: palette.surface,
                color: palette.fg, fontFamily: fonts.sans, fontSize: 15, textAlignVertical: "top" }}
            />
            {promptTooLong ? <Text accessibilityRole="alert" style={{ color: palette.alert }}>The first message is too long.</Text> : null}
            {unsafeClaudePrompt ? <Text accessibilityRole="alert" style={{ color: palette.alert }}>Write a few more words: one word could run a Claude command.</Text> : null}
          </View>
        ) : null}
        {busy ? <Text accessibilityLiveRegion="polite" style={{ color: palette.fgSoft }}>
          {pending?.phase === "starting" ? `Starting on ${macName}…`
            : pending?.phase === "delayed"
              ? `Terminal opened on ${macName}. If your session is not in Chats yet, check Terminal on that Mac.`
              : `Terminal opened on ${macName}. Your session will appear in Chats when it is shared.`}
        </Text> : null}
        {(pending?.phase === "opened" || pending?.phase === "delayed") ? (
          <Pressable accessibilityRole="button" accessibilityLabel="View sessions" onPress={() => router.replace("/(app)/(tabs)/chats")}>
            <Text style={{ color: palette.link, fontFamily: fonts.sansMedium }}>View sessions in Chats</Text>
          </Pressable>
        ) : null}
        {pending?.phase === "failed" ? <Text accessibilityRole="alert" style={{ color: palette.alert }}>{pending.error}</Text> : null}
        {options?.sharing && selected && folderPath ? (
          <Pressable onPress={() => void start()} disabled={!canStart} accessibilityRole="button"
            accessibilityLabel="Start session" accessibilityState={{ disabled: !canStart }}
            style={{ minHeight: 48, alignItems: "center", justifyContent: "center", borderRadius: radii.md,
              backgroundColor: canStart ? palette.fg : palette.selected }}>
            <Text style={{ color: canStart ? palette.onFg : palette.fgMute, fontFamily: fonts.sansMedium, fontSize: 15 }}>Start session</Text>
          </Pressable>
        ) : null}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}
