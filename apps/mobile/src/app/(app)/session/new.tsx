import { useAuth } from "@clerk/clerk-expo";
import { ApiError } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { router, Stack, useFocusEffect } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Keyboard, Pressable, ScrollView, Text, TextInput, useColorScheme, View } from "react-native";
import { KeyboardAvoidingView } from "react-native-keyboard-controller";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { SegmentedControl } from "../../../components/ui/SegmentedControl";
import { BottomSheet } from "../../../components/ui/BottomSheet";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { usePushedScreenKeyboardOffset } from "../../../lib/keyboardOffset";
import { LocalStore } from "../../../lib/localStore";
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
  startedAt: number;
  phase: "starting" | "opened" | "delayed" | "unknown" | "failed";
  error?: string;
};
const startStore = new LocalStore<{ userId?: string; ownerScope?: string; startId?: string; startedAt?: number }>("session-start");

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
  const { userId, orgId, isLoaded } = useAuth();
  const ownerScope = orgId ?? userId;
  const api = useDesktopSessionApi();
  const live = useLiveEvents();
  const [targets, setTargets] = useState<SessionTargets | null>(null);
  const [targetsError, setTargetsError] = useState<string | null>(null);
  const [deviceId, setDeviceId] = useState("");
  const [optionsRefresh, setOptionsRefresh] = useState(0);
  const [optionsState, setOptionsState] = useState<{ key: string; options?: SessionOptions; error?: string } | null>(null);
  const [tool, setTool] = useState<"claude" | "codex" | "">("");
  const [folder, setFolder] = useState("");
  const [model, setModel] = useState("");
  const [mode, setMode] = useState("");
  const [effort, setEffort] = useState("");
  const [prompt, setPrompt] = useState("");
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const openedRef = useRef<string | null>(null);
  const targetRequest = useRef(0);
  const recordPending = useCallback((next: Pending) => {
    pendingRef.current = next;
    setPending(next);
    startStore.write(next.phase === "failed" ? {} : {
      userId: userId ?? undefined, ownerScope: ownerScope ?? undefined,
      startId: next.startId, startedAt: next.startedAt,
    });
    startStore.flush();
  }, [userId, ownerScope]);

  useEffect(() => {
    if (!isLoaded || !userId || !ownerScope) return;
    const saved = startStore.read();
    if (saved.startId && (saved.userId !== userId || saved.ownerScope !== ownerScope)) {
      pendingRef.current = null;
      setPending(null);
      startStore.write({});
      startStore.flush();
      return;
    }
    if (pendingRef.current) return;
    if (saved.userId === userId && saved.ownerScope === ownerScope && saved.startId && typeof saved.startedAt === "number"
      && Date.now() - saved.startedAt < 6 * 60 * 60 * 1000) {
      recordPending({ startId: saved.startId, startedAt: saved.startedAt, phase: "unknown" });
    } else if (saved.startId) {
      startStore.write({});
      startStore.flush();
    }
  }, [isLoaded, userId, ownerScope, recordPending]);

  const openExactSession = useCallback(async (startId: string) => {
    try {
      const response = await api.get("/messaging/sessions") as { sessions?: { startId?: string; sessionId: string; humanUserId: string }[] };
      const exact = response.sessions?.find((item) => item.startId === startId && item.humanUserId === userId);
      if (!exact || pendingRef.current?.startId !== startId || openedRef.current === startId) return;
      openedRef.current = startId;
      startStore.write({});
      startStore.flush();
      router.replace({ pathname: "/(app)/session/[sessionId]", params: { sessionId: exact.sessionId } });
    } catch { /* A missed list refresh is retried on the next hint or poll. */ }
  }, [api, userId]);

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
  const modelValue = selected?.models?.some((candidate) => candidate.id === model) ? model : "";
  const modeValue = selected?.modes?.some((candidate) => candidate.id === mode)
    ? mode : selected?.defaultMode ?? selected?.modes?.[0]?.id ?? "";
  const effortValue = selected?.efforts?.some((candidate) => candidate.id === effort)
    ? effort : selected?.defaultEffort ?? "";
  const promptTooLong = byteLength(prompt) > MAX_PROMPT_BYTES;
  const unsafeClaudePrompt = selected?.id === "claude" && /^[a-z][a-z-]*$/.test(prompt.trim());
  const busy = !!pending && pending.phase !== "failed";

  useEffect(() => {
    const unsubscribe = live.onEvent((event) => {
      const current = pendingRef.current;
      if (!current) return;
      if (event.type === "workspace.sessions.changed") { void openExactSession(current.startId); return; }
      if (event.type !== "runtime.operation.changed" || event.payload.operationId !== current.startId) return;
      if (event.payload.state === "failed") recordPending(event.payload.error?.retryable === false
        ? { ...current, phase: "failed", error: sessionOutcomeError(event.payload.error) }
        : { ...current, phase: "unknown" });
      if (event.payload.state === "succeeded") recordPending({ ...current, phase: "opened", error: undefined });
    });
    return () => { unsubscribe(); };
  }, [live.onEvent, openExactSession, recordPending]);
  useEffect(() => {
    if (!pending || pending.phase === "failed") return;
    void openExactSession(pending.startId);
    const timer = setInterval(() => void openExactSession(pending.startId), 10_000);
    return () => clearInterval(timer);
  }, [pending?.startId, pending?.phase, openExactSession]);
  useEffect(() => {
    if (!pending || pending.phase === "failed" || pending.phase === "delayed" || pending.phase === "unknown") return;
    const timer = setTimeout(() => {
      const current = pendingRef.current;
      if (!current || current.startId !== pending.startId) return;
      recordPending({ ...current, phase: current.phase === "opened" ? "delayed" : "unknown" });
    }, PENDING_MS);
    return () => clearTimeout(timer);
  }, [pending?.startId, pending?.phase, recordPending]);

  const blocked = targetsError
    ?? (!targets ? null
      : !targets.enabled ? "Starting sessions is off for this workspace."
      : !mac ? "No Mac is ready. Open the Lightbulb desktop app on your Mac and sign in."
      : optionsError ? `Can't reach ${macName}. ${optionsError}`
      : !options ? null
      : !options.sharing ? `Session sharing is off on ${macName}. Turn it on in the Lightbulb desktop app.`
      : options.tools.some((item) => !item.defaultMode || !Array.isArray(item.efforts))
        ? `Update Lightbulb on ${macName} to choose safe session settings.`
      : options.tools.length === 0 ? `Install Claude Code or Codex on ${macName} first.`
      : options.folders.length === 0 ? "Approve a folder in the desktop app: Settings → File access."
      : null);
  const ready = !!userId && !!mac && !!options && !!selected && !!folderPath && !blocked;
  const canStart = ready && !busy && !!prompt.trim() && !promptTooLong && !unsafeClaudePrompt;

  async function start() {
    if (!canStart || !mac || !selected) return;
    Keyboard.dismiss();
    const startId = randomUUID();
    recordPending({ startId, startedAt: Date.now(), phase: "starting" });
    try {
      await api.post("/desktop-sessions/start?source=ios", {
        startId, deviceId: mac.deviceId, generation: mac.generation,
        tool: selected.id, folder: folderPath, model: modelValue || null, mode: modeValue || null,
        effort: effortValue || null, prompt,
      });
    } catch (error) {
      const current = pendingRef.current;
      if (current?.startId !== startId || current.phase !== "starting") return;
      // A dropped 202/5xx cannot prove the Mac did not launch. Keep retry disabled.
      recordPending(error instanceof ApiError && error.status < 500
        ? { ...current, phase: "failed", error: sessionStartError(error) }
        : { ...current, phase: "unknown" });
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
        {targets?.enabled && targets.devices.length > 1 && !(options?.sharing && selected && folderPath) && !busy ? (
          <Pressable accessibilityRole="button" accessibilityLabel="Choose another Mac" onPress={() => setOptionsOpen(true)}>
            <Text style={{ color: palette.link, fontFamily: fonts.sansMedium }}>Choose another Mac</Text>
          </Pressable>
        ) : null}
        {options?.sharing && selected && folderPath && !busy ? (
          <View style={{ gap: spacing.md }}>
            <TextInput
              accessibilityLabel="First message"
              placeholder="What would you like to work on?"
              placeholderTextColor={palette.fgMute}
              multiline
              autoFocus
              value={prompt}
              onChangeText={setPrompt}
              style={{ minHeight: 152, padding: spacing.md, borderRadius: radii.md,
                borderWidth: 1, borderColor: palette.edge, backgroundColor: palette.surface,
                color: palette.fg, fontFamily: fonts.sans, fontSize: 17, textAlignVertical: "top" }}
            />
            {promptTooLong ? <Text accessibilityRole="alert" style={{ color: palette.alert }}>The first message is too long.</Text> : null}
            {unsafeClaudePrompt ? <Text accessibilityRole="alert" style={{ color: palette.alert }}>Write a few more words: one word could run a Claude command.</Text> : null}
            <Pressable onPress={() => void start()} disabled={!canStart} accessibilityRole="button"
              accessibilityLabel="Start session" accessibilityState={{ disabled: !canStart }}
              style={{ minHeight: 48, alignItems: "center", justifyContent: "center", borderRadius: radii.md,
                backgroundColor: canStart ? palette.fg : palette.selected }}>
              <Text style={{ color: canStart ? palette.onFg : palette.fgMute, fontFamily: fonts.sansMedium, fontSize: 15 }}>Send to Mac</Text>
            </Pressable>
            <Pressable accessibilityRole="button" accessibilityLabel="Session options" onPress={() => setOptionsOpen(true)}
              style={{ minHeight: 44, justifyContent: "center" }}>
              <Text style={{ color: palette.fgSoft, fontFamily: fonts.sans, fontSize: 14 }}>
                {selected.label} · {shortPath(folderPath)} · {macName}  Change
              </Text>
            </Pressable>
          </View>
        ) : null}
        {mac && !options && !optionsError ? <ActivityIndicator accessibilityLabel="Checking your Mac" /> : null}
        {busy ? <Text accessibilityLiveRegion="polite" style={{ color: palette.fgSoft }}>
          {pending?.phase === "starting" ? `Starting on ${macName}…`
            : pending?.phase === "unknown"
              ? `Could not confirm whether ${macName} started the session. Check Terminal and Chats before trying again.`
            : pending?.phase === "delayed"
              ? `Terminal opened on ${macName}. If your session is not in Chats yet, check Terminal on that Mac.`
              : `Terminal opened on ${macName}. Your session will appear in Chats when it is shared.`}
        </Text> : null}
        {(pending?.phase === "opened" || pending?.phase === "delayed" || pending?.phase === "unknown") ? (
          <Pressable accessibilityRole="button" accessibilityLabel="View sessions" onPress={() => router.dismissTo("/(app)/(tabs)/chats?filter=sessions")}>
            <Text style={{ color: palette.link, fontFamily: fonts.sansMedium }}>View sessions in Chats</Text>
          </Pressable>
        ) : null}
        {pending?.phase === "failed" ? <Text accessibilityRole="alert" style={{ color: palette.alert }}>{pending.error}</Text> : null}
      </ScrollView>
      <BottomSheet visible={optionsOpen && !busy} onClose={() => setOptionsOpen(false)} large>
        <ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={{ padding: spacing.lg, paddingBottom: insets.bottom + spacing.xl, gap: spacing.lg }}>
          <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium, fontSize: 20 }}>Session options</Text>
          {targets?.enabled && targets.devices.length > 1 ? (
            <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
              <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Mac</Text>
              {targets.devices.map((device, index) => choice(device.deviceId,
                device.deviceId === mac?.deviceId ? macName : device.name === "Your Mac" ? `Your Mac ${index + 1}` : device.name,
                device.deviceId === mac?.deviceId,
                () => { setDeviceId(device.deviceId); setFolder(""); setTool(""); setModel(""); setMode(""); setEffort(""); },
              ))}
            </View>
          ) : null}
          {options?.sharing && options.tools.length > 0 ? (
            <View style={{ gap: spacing.sm }}>
              <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Tool</Text>
              <SegmentedControl label="CLI tool" segments={options.tools.map((item) => ({ key: item.id, label: item.label }))}
                value={selected!.id} onChange={(next) => { setTool(next); setModel(""); setMode(""); setEffort(""); }} colorScheme={dark ? "dark" : "light"} />
            </View>
          ) : null}
          {options?.sharing && options.folders.length > 0 ? (
            <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
              <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Folder</Text>
              {options.folders.map((path) => choice(path, shortPath(path), path === folderPath, () => setFolder(path)))}
            </View>
          ) : null}
          {selected?.models?.length ? (
            <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
              <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Model</Text>
              {choice("default-model", "Mac default", modelValue === "", () => setModel(""))}
              {selected.models.map((item) => choice(item.id, item.label, modelValue === item.id, () => setModel(item.id)))}
            </View>
          ) : null}
          {selected?.efforts?.length ? (
            <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
              <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>Reasoning effort</Text>
              {choice("default-effort", "Mac default", effortValue === "", () => setEffort(""))}
              {selected.efforts.map((item) => choice(item.id, item.label, effortValue === item.id, () => setEffort(item.id)))}
            </View>
          ) : null}
          {selected?.modes?.length ? (
            <View style={{ gap: spacing.sm }} accessibilityRole="radiogroup">
              <Text accessibilityRole="header" style={{ color: palette.fg, fontFamily: fonts.sansMedium }}>{selected.modeLabel ?? "Permission mode"}</Text>
              {selected.modes.map((item) => choice(item.id, item.label, modeValue === item.id, () => setMode(item.id)))}
            </View>
          ) : null}
          <Pressable accessibilityRole="button" accessibilityLabel="Done with session options" onPress={() => setOptionsOpen(false)}
            style={{ minHeight: 48, alignItems: "center", justifyContent: "center", borderRadius: radii.md, backgroundColor: palette.fg }}>
            <Text style={{ color: palette.onFg, fontFamily: fonts.sansMedium }}>Done</Text>
          </Pressable>
        </ScrollView>
      </BottomSheet>
    </KeyboardAvoidingView>
  );
}
